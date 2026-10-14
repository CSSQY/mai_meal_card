"""DeepSeek 开放平台适配器。

接口（官方文档核实：https://api-docs.deepseek.com/zh-cn/api/get-user-balance）：
    GET {base_url}/user/balance
    Accept: application/json
    Authorization: Bearer <API Key>

响应示例：
    {
      "is_available": true,
      "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00",
         "granted_balance": "10.00", "topped_up_balance": "100.00"}
      ]
    }

余额按币种区分：``balance_infos`` 可能包含多条（如人民币 + 美元）。第一条按标准指标 id
产出（``balance_total`` 等），其余条目追加币种后缀（如 ``balance_total_usd``）避免冲突；
币种（``CNY`` / ``USD``）作为展示单位。

防御式解析：字段缺失只跳过对应指标、金额解析失败保留截断后的原始响应，
绝不抛异常、绝不静默填 0。
"""

from typing import Any, Dict, List, Optional, Tuple

import asyncio
import json
import logging

from .base import KIND_COUNT, KIND_TEXT, MetricValue, mask_sensitive

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_BALANCE_PATH = "/user/balance"
_ERROR_BODY_LIMIT = 300
_RAW_DISPLAY_LIMIT = 300

# (响应字段, 指标 id, 中文名)
_AMOUNT_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("total_balance", "balance_total", "账户总余额"),
    ("granted_balance", "balance_granted", "赠送余额"),
    ("topped_up_balance", "balance_topped_up", "充值余额"),
)


def _to_amount(raw: Any) -> Optional[float]:
    """把余额字段（字符串小数，如 ``"110.00"``）转为数值；无法解析时返回 None。"""

    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _truncate(text: str, limit: int = _ERROR_BODY_LIMIT) -> str:
    """截断长文本，避免日志被淹没。"""

    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}…（已截断）"


def _failure(metric_id: str, name: str, error: str) -> MetricValue:
    """构造一个失败指标。"""

    return MetricValue(id=metric_id, name=name, kind=KIND_TEXT, value=None, display="查询失败", ok=False, error=error)


class DeepSeekAdapter:
    """DeepSeek 开放平台适配器（账户余额查询）。"""

    id = "deepseek"
    display_name = "DeepSeek 开放平台"

    def is_configured(self, slot: Any) -> Tuple[bool, str]:
        """检查是否已填写 API Key。"""

        api_key = str(getattr(slot, "api_key", "") or "").strip()
        if not api_key:
            return False, "未填写 DeepSeek API Key"
        return True, ""

    def metric_ids(self) -> List[str]:
        """返回主要指标 id（多币种时的 ``balance_*_<币种>`` 为动态指标，不在此列出）。"""

        return ["balance_total", "balance_granted", "balance_topped_up", "available", "raw"]

    def describe(self, slot: Any) -> str:
        """返回该槽位的中文描述（基地址被覆盖时带上地址）。"""

        override = str(getattr(slot, "base_url", "") or "").strip()
        if override:
            return f"{self.display_name}（{override.rstrip('/')}）"
        return self.display_name

    def base_url(self, slot: Any) -> str:
        """解析基地址：优先槽位覆盖，否则用官方地址。"""

        override = str(getattr(slot, "base_url", "") or "").strip()
        return override.rstrip("/") or _DEFAULT_BASE_URL

    async def fetch(self, slot: Any, *, timeout: float, retries: int) -> List[MetricValue]:
        """查询账户余额并解析；本函数不抛异常。"""

        configured, reason = self.is_configured(slot)
        if not configured:
            return [_failure("config", "未配置", reason)]

        api_key = str(slot.api_key).strip()
        url = f"{self.base_url(slot)}{_BALANCE_PATH}"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

        try:
            body_text = await self._request(url, headers, timeout=timeout, retries=retries)
        except Exception as exc:
            logger.error("[meal_card] DeepSeek 查询失败：%s", exc)
            return [_failure("raw", "查询失败", str(exc))]

        try:
            payload = json.loads(body_text)
        except Exception as exc:
            logger.error("[meal_card] DeepSeek 响应不是合法 JSON：%s；原文：%s", exc, _truncate(body_text))
            return [_failure("raw", "响应解析失败", f"响应不是合法 JSON：{exc}")]

        if not isinstance(payload, dict):
            return [_failure("raw", "响应解析失败", f"响应结构异常：{type(payload).__name__}")]

        metrics = self._build_metrics(payload)
        if not metrics:
            logger.warning(
                "[meal_card] DeepSeek 响应中没有可用的余额字段，原文：%s",
                _truncate(json.dumps(payload, ensure_ascii=False)),
            )
            return [_failure("raw", "响应解析失败", "响应中缺少 is_available 与 balance_infos 字段")]

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

    @staticmethod
    def _build_metrics(payload: Dict[str, Any]) -> List[MetricValue]:
        """把响应体解析为指标列表（跳过缺失/非法字段）。"""

        metrics: List[MetricValue] = []

        available = payload.get("is_available")
        if isinstance(available, bool):
            metrics.append(
                MetricValue(
                    id="available",
                    name="账户可用状态",
                    kind=KIND_TEXT,
                    value=None,
                    display="可用" if available else "不可用",
                    ok=True,
                )
            )

        entries = payload.get("balance_infos")
        valid_entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
        for index, entry in enumerate(valid_entries):
            currency = str(entry.get("currency") or "").strip().upper()
            suffix = "" if index == 0 else f"_{currency.lower()}" if currency else f"_{index + 1}"
            label = "" if index == 0 else f"（{currency or index + 1}）"
            for field, metric_id, name in _AMOUNT_FIELDS:
                amount = _to_amount(entry.get(field))
                if amount is None:
                    continue
                metrics.append(
                    MetricValue(
                        id=f"{metric_id}{suffix}",
                        name=f"{name}{label}",
                        kind=KIND_COUNT,
                        value=amount,
                        unit=currency,
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
                            "401 未授权：请确认填写的是 DeepSeek 开放平台 API Key（sk- 开头、未过期）"
                        )
                    if response.status_code >= 500 or response.status_code == 429:
                        last_error = f"HTTP {response.status_code}：{detail}"
                    else:
                        raise RuntimeError(f"HTTP {response.status_code}：{detail}")

                if attempt < attempts:
                    logger.warning(
                        "[meal_card] DeepSeek 请求失败（第 %d/%d 次）：%s；稍后重试",
                        attempt,
                        attempts,
                        last_error,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 5.0))

        raise RuntimeError(f"请求失败（共尝试 {attempts} 次）：{last_error}")