"""MiniMax TokenPlan 平台适配器。

接口（官方文档核实）：
    GET {host}/v1/token_plan/remains
    Authorization: Bearer <订阅 Key>
    Content-Type: application/json

区域：
    cn     → https://www.minimax.cn   （国内版）
    global → https://www.minimax.io   （国际版）

额度模型：5 小时滚动窗口 + 周窗口，文本/图像/语音共享同一份额度。

**注意**：该接口的响应字段未在官方文档中承诺，本适配器采用防御式解析：
字段缺失只跳过对应指标、解析失败保留截断后的原始响应，绝不抛异常、绝不静默填 0。
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncio
import json
import logging

from .base import (
    KIND_COUNT,
    KIND_DURATION,
    KIND_PERCENT,
    KIND_TEXT,
    MetricValue,
    mask_sensitive,
)

logger = logging.getLogger(__name__)

_REMAINS_PATH = "/v1/token_plan/remains"
_ERROR_BODY_LIMIT = 300
_RAW_DISPLAY_LIMIT = 300

_REGION_BASE_URLS: Dict[str, str] = {
    "cn": "https://www.minimax.cn",
    "global": "https://www.minimax.io",
}

_REGION_LABELS: Dict[str, str] = {
    "cn": "国内版",
    "global": "国际版",
}

_OVERALL_MODEL_NAME = "general"
"""代表「通用/总体额度池」的条目名（文本/图像/语音共享的那一份额度）。"""

_INTERVAL_PERCENT_KEYS: Tuple[str, ...] = (
    "current_interval_remaining_percent",
    "interval_remaining_percent",
    "current_interval_remaining_percent_value",
)
_WEEKLY_PERCENT_KEYS: Tuple[str, ...] = (
    "current_weekly_remaining_percent",
    "weekly_remaining_percent",
)
_INTERVAL_RESET_KEYS: Tuple[str, ...] = (
    "remains_time",
    "current_interval_remains_time",
    "interval_remains_time",
)
_WEEKLY_RESET_KEYS: Tuple[str, ...] = (
    "current_weekly_remains_time",
    "weekly_remains_time",
)
_TOTAL_COUNT_KEYS: Tuple[str, ...] = (
    "current_interval_total_count",
    "interval_total_count",
)
_USAGE_COUNT_KEYS: Tuple[str, ...] = (
    "current_interval_usage_count",
    "interval_usage_count",
)


def _first_number(source: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    """按候选字段名顺序取第一个可解析为数值的值。"""

    for key in keys:
        if key not in source:
            continue
        raw = source.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _truncate(text: str, limit: int = _ERROR_BODY_LIMIT) -> str:
    """截断长文本，避免日志被淹没。"""

    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}…（已截断）"


def _pick_primary(entries: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """选出代表「总体额度」的条目：优先 general，其次剩余最少的一条。"""

    for entry in entries:
        if str(entry.get("model_name") or "").strip().lower() == _OVERALL_MODEL_NAME:
            return entry

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for entry in entries:
        remaining = _first_number(entry, _INTERVAL_PERCENT_KEYS)
        if remaining is not None:
            scored.append((remaining, entry))
    if scored:
        return min(scored, key=lambda item: item[0])[1]
    return entries[0] if entries else None


def _failure(metric_id: str, name: str, error: str) -> MetricValue:
    """构造一个失败指标。"""

    return MetricValue(id=metric_id, name=name, kind=KIND_TEXT, value=None, display="查询失败", ok=False, error=error)


class MiniMaxAdapter:
    """MiniMax TokenPlan 适配器。"""

    id = "minimax"
    display_name = "MiniMax TokenPlan"

    def is_configured(self, slot: Any) -> Tuple[bool, str]:
        """检查是否已填写订阅密钥。"""

        api_key = str(getattr(slot, "api_key", "") or "").strip()
        if not api_key:
            return False, "未填写 MiniMax 订阅密钥"
        return True, ""

    def metric_ids(self) -> List[str]:
        """返回主要指标 id（按模型拆分的 ``model_*_remaining`` 为动态指标，不在此列出）。"""

        return [
            "interval_remaining",
            "interval_reset",
            "weekly_remaining",
            "weekly_reset",
            "interval_usage",
            "raw",
        ]

    def describe(self, slot: Any) -> str:
        """返回该槽位的中文描述（含区域）。"""

        region = str(getattr(slot, "region", "cn") or "cn").strip().lower()
        label = _REGION_LABELS.get(region, region)
        return f"{self.display_name}（{label}）"

    def base_url(self, slot: Any) -> str:
        """解析基地址：优先槽位覆盖，其次按区域映射。"""

        override = str(getattr(slot, "base_url", "") or "").strip()
        if override:
            return override.rstrip("/")
        region = str(getattr(slot, "region", "cn") or "cn").strip().lower()
        return _REGION_BASE_URLS.get(region, _REGION_BASE_URLS["cn"])

    async def fetch(self, slot: Any, *, timeout: float, retries: int) -> List[MetricValue]:
        """查询并解析 TokenPlan 用量；本函数不抛异常。"""

        configured, reason = self.is_configured(slot)
        if not configured:
            return [_failure("config", "未配置", reason)]

        api_key = str(slot.api_key).strip()
        url = f"{self.base_url(slot)}{_REMAINS_PATH}"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        try:
            body_text = await self._request(url, headers, timeout=timeout, retries=retries)
        except Exception as exc:
            logger.error("[meal_card] MiniMax 查询失败：%s", exc)
            return [_failure("raw", "查询失败", str(exc))]

        try:
            payload = json.loads(body_text)
        except Exception as exc:
            logger.error("[meal_card] MiniMax 响应不是合法 JSON：%s；原文：%s", exc, _truncate(body_text))
            return [_failure("raw", "响应解析失败", f"响应不是合法 JSON：{exc}")]

        if not isinstance(payload, dict):
            return [_failure("raw", "响应解析失败", f"响应结构异常：{type(payload).__name__}")]

        base_resp = payload.get("base_resp")
        if isinstance(base_resp, dict):
            status_code = base_resp.get("status_code")
            if status_code not in (None, 0, "0"):
                message = str(base_resp.get("status_msg") or "").strip()
                return [_failure("raw", "接口返回错误", f"status_code={status_code} {message}".strip())]

        entries = payload.get("model_remains")
        if not isinstance(entries, list) or not entries:
            logger.warning(
                "[meal_card] MiniMax 响应缺少可用的 model_remains，原文：%s",
                _truncate(json.dumps(payload, ensure_ascii=False)),
            )
            return [_failure("raw", "响应解析失败", "响应中缺少 model_remains 列表")]

        valid_entries = [entry for entry in entries if isinstance(entry, dict)]
        metrics: List[MetricValue] = []
        metrics.extend(self._build_overall_metrics(valid_entries))
        metrics.extend(self._build_model_metrics(valid_entries))
        metrics.append(
            MetricValue(
                id="raw",
                name="原始响应",
                kind=KIND_TEXT,
                value=None,
                display=_truncate(json.dumps(mask_sensitive(payload), ensure_ascii=False), _RAW_DISPLAY_LIMIT),
                ok=True,
            )
        )
        return metrics

    async def _request(self, url: str, headers: Dict[str, str], *, timeout: float, retries: int) -> str:
        """带重试地发起一次 GET 请求，返回响应体文本。"""

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 宿主已依赖 httpx
            raise RuntimeError(f"缺少 httpx 依赖，无法发起查询：{exc}") from exc

        attempts = max(int(retries), 0) + 1
        last_error = "未发起任何请求"

        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.get(url, headers=headers)
                except Exception as exc:
                    last_error = f"网络异常：{exc}"
                else:
                    if response.status_code < 400:
                        return response.text
                    detail = _truncate(response.text)
                    if response.status_code == 401:
                        raise RuntimeError(
                            "401 未授权：请确认「区域」与套餐购买地区一致，且填写的是 TokenPlan 订阅密钥"
                            "（订阅密钥与按量付费 API Key 不通用）"
                        )
                    if response.status_code >= 500 or response.status_code == 429:
                        last_error = f"HTTP {response.status_code}：{detail}"
                    else:
                        raise RuntimeError(f"HTTP {response.status_code}：{detail}")

                if attempt < attempts:
                    logger.warning(
                        "[meal_card] MiniMax 请求失败（第 %d/%d 次）：%s；稍后重试",
                        attempt,
                        attempts,
                        last_error,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 5.0))

        raise RuntimeError(f"请求失败（共尝试 {attempts} 次）：{last_error}")

    @staticmethod
    def _build_overall_metrics(entries: Sequence[Dict[str, Any]]) -> List[MetricValue]:
        """构造总体指标（5 小时窗口 / 周窗口）。"""

        metrics: List[MetricValue] = []
        primary = _pick_primary(entries)
        if primary is None:
            return metrics

        interval_remaining = _first_number(primary, _INTERVAL_PERCENT_KEYS)
        if interval_remaining is not None:
            metrics.append(
                MetricValue(
                    id="interval_remaining",
                    name="5h窗口剩余",
                    kind=KIND_PERCENT,
                    value=interval_remaining,
                    progress=min(max(interval_remaining / 100.0, 0.0), 1.0),
                )
            )

        interval_reset = _first_number(primary, _INTERVAL_RESET_KEYS)
        if interval_reset is not None:
            metrics.append(
                MetricValue(id="interval_reset", name="5h窗口重置倒计时", kind=KIND_DURATION, value=interval_reset)
            )

        weekly_remaining = _first_number(primary, _WEEKLY_PERCENT_KEYS)
        if weekly_remaining is not None:
            metrics.append(
                MetricValue(
                    id="weekly_remaining",
                    name="本周窗口剩余",
                    kind=KIND_PERCENT,
                    value=weekly_remaining,
                    progress=min(max(weekly_remaining / 100.0, 0.0), 1.0),
                )
            )

        weekly_reset = _first_number(primary, _WEEKLY_RESET_KEYS)
        if weekly_reset is not None:
            metrics.append(
                MetricValue(id="weekly_reset", name="周窗口重置倒计时", kind=KIND_DURATION, value=weekly_reset)
            )

        total_count = _first_number(primary, _TOTAL_COUNT_KEYS)
        usage_count = _first_number(primary, _USAGE_COUNT_KEYS)
        if total_count is not None and total_count > 0 and usage_count is not None:
            metrics.append(
                MetricValue(
                    id="interval_usage",
                    name="5h窗口已用次数",
                    kind=KIND_COUNT,
                    value=usage_count,
                    progress=min(max(usage_count / total_count, 0.0), 1.0),
                )
            )

        return metrics

    @staticmethod
    def _build_model_metrics(entries: Sequence[Dict[str, Any]]) -> List[MetricValue]:
        """按模型拆分剩余百分比指标（``model_<名称>_remaining``）。

        **跳过 ``general``**：它代表「通用/总体额度池」，已由「5h窗口剩余 / 本周窗口剩余」
        表达；若再拆成一个模型指标，会与总体指标重复。
        """

        metrics: List[MetricValue] = []
        for entry in entries:
            model_name = str(entry.get("model_name") or "").strip()
            if not model_name or model_name.lower() == _OVERALL_MODEL_NAME:
                continue
            remaining = _first_number(entry, _INTERVAL_PERCENT_KEYS)
            if remaining is None:
                continue
            metrics.append(
                MetricValue(
                    id=f"model_{model_name}_remaining",
                    name=f"{model_name} 剩余",
                    kind=KIND_PERCENT,
                    value=remaining,
                    progress=min(max(remaining / 100.0, 0.0), 1.0),
                )
            )
        return metrics
