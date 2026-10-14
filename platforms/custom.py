"""自定义平台适配器。

用于适配「尚未内置适配」的平台：在平台槽位里把「平台类型」选为 ``custom``，
再填写接口地址、鉴权、取值表达式等，插件即可按通用规则取值并展示。

取值流程：
1. 拼出 ``{基地址}{路径}``；
2. 按「鉴权方式」生成请求头（none / bearer / header / basic），并叠加「附加请求头」；
3. GET / POST 请求（POST 时把「请求体」按 JSON 解析，解析失败按原始文本发送）；
4. 按「取值表达式」从 JSON 取点号路径（留空则整段响应文本转数值）；
5. 按「换算比例 a:b」换算（a 原始单位 = b 显示单位）；
6. 「上限表达式」非空时再取一次上限，算出用量进度。
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncio
import json
import logging
import re

from .base import KIND_COUNT, KIND_PERCENT, KIND_TEXT, MetricValue, mask_sensitive

logger = logging.getLogger(__name__)

_ERROR_BODY_LIMIT = 200
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _truncate(text: str, limit: int = _ERROR_BODY_LIMIT) -> str:
    """截断长文本。"""

    normalized = str(text or "").strip()
    return normalized if len(normalized) <= limit else f"{normalized[:limit]}…（已截断）"


def _dig(obj: Any, path: str) -> Any:
    """按点号路径从嵌套结构里取值（支持 dict 键与 list 下标）。"""

    if not path:
        return obj
    current = obj
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise ValueError(f"响应中不存在字段：{path}（缺 {part}）")
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise ValueError(f"取值路径非法：{path}（{part} 不是下标）") from exc
            if not 0 <= index < len(current):
                raise ValueError(f"取值下标越界：{path}")
            current = current[index]
        else:
            raise ValueError(f"响应结构在 {part} 处不可继续下钻：{path}")
    return current


def _to_float(raw: Any) -> float:
    """把取值结果转为浮点数。"""

    if isinstance(raw, bool):
        raise ValueError("取值结果是布尔值，无法作为数值使用")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        matched = _NUMBER_PATTERN.search(raw.replace(",", ""))
        if matched is None:
            raise ValueError(f"取值结果不是数字：{raw!r}")
        return float(matched.group(0))
    raise ValueError(f"取值结果类型不支持：{type(raw).__name__}")


def _extract_number(body_text: str, expr: str) -> float:
    """按取值表达式从响应体取数值；表达式留空时整段文本转数值。"""

    if not expr.strip():
        return _to_float(body_text)
    try:
        payload = json.loads(body_text)
    except Exception as exc:
        raise ValueError(f"响应不是合法 JSON，无法按表达式取值：{exc}") from exc
    return _to_float(_dig(payload, expr.strip()))


def _masked_body_text(body_text: str) -> str:
    """自定义平台 raw 脱敏：能解析为 JSON 就按敏感键名脱敏，否则原样返回。

    自定义平台返回的可能是纯文本，此时无法按键名脱敏，只做截断。
    """

    try:
        parsed = json.loads(body_text)
    except Exception:
        return body_text
    return json.dumps(mask_sensitive(parsed), ensure_ascii=False)


def _parse_ratio(raw: str) -> Tuple[float, float]:
    """解析换算比例 ``a:b``；非法或留空按 ``1:1``。"""

    text = str(raw or "").strip()
    if not text or ":" not in text:
        return 1.0, 1.0
    left, right = text.split(":", 1)
    try:
        a = float(left.strip())
        b = float(right.strip())
    except ValueError:
        logger.warning("[meal_card] 自定义平台换算比例不是数字，已按 1:1 处理：%s", raw)
        return 1.0, 1.0
    if a == 0:
        logger.warning("[meal_card] 自定义平台换算比例 a 为 0，已按 1:1 处理：%s", raw)
        return 1.0, 1.0
    return a, b


def _parse_pairs(items: Sequence[str]) -> Dict[str, str]:
    """把 ``["名=值", ...]`` 解析为字典。"""

    pairs: Dict[str, str] = {}
    for item in items:
        text = str(item or "").strip()
        if not text or "=" not in text:
            if text:
                logger.warning("[meal_card] 自定义平台请求头格式非法（应为 名=值），已忽略：%s", text)
            continue
        key, value = text.split("=", 1)
        if key.strip():
            pairs[key.strip()] = value.strip()
    return pairs


def _failure(metric_id: str, name: str, error: str) -> MetricValue:
    """构造一个失败指标。"""

    return MetricValue(id=metric_id, name=name, kind=KIND_TEXT, value=None, display="查询失败", ok=False, error=error)


class CustomAdapter:
    """自定义平台适配器。"""

    id = "custom"
    display_name = "自定义平台"

    def is_configured(self, slot: Any) -> Tuple[bool, str]:
        """检查必填项：接口基地址。"""

        base_url = str(getattr(slot, "custom_base_url", "") or "").strip()
        if not base_url:
            return False, "未填写接口基地址"
        return True, ""

    def metric_ids(self) -> List[str]:
        """自定义平台固定产出一个 ``value`` 指标（另有 ``raw``）。"""

        return ["value", "raw"]

    def describe(self, slot: Any) -> str:
        """返回该槽位的中文描述（含接口地址）。"""

        base_url = str(getattr(slot, "custom_base_url", "") or "").strip()
        return f"{self.display_name}（{base_url}）" if base_url else self.display_name

    async def fetch(self, slot: Any, *, timeout: float, retries: int) -> List[MetricValue]:
        """按槽位配置发起查询并解析；本函数不抛异常。"""

        configured, reason = self.is_configured(slot)
        if not configured:
            return [_failure("config", "未配置", reason)]

        url = self._build_url(slot)
        headers = self._build_headers(slot)
        body_text = await self._request(slot, url, headers, timeout=timeout, retries=retries)
        if isinstance(body_text, MetricValue):
            return [body_text]

        value_expr = str(getattr(slot, "custom_value_expr", "") or "")
        ratio_a, ratio_b = _parse_ratio(str(getattr(slot, "custom_ratio", "") or ""))
        unit = str(getattr(slot, "custom_unit", "") or "").strip()
        value_type = str(getattr(slot, "custom_value_type", "quota") or "quota").strip().lower()

        try:
            raw_value = _extract_number(body_text, value_expr)
        except ValueError as exc:
            logger.error("[meal_card] 自定义平台取值失败：%s", exc)
            return [_failure("value", "取值失败", str(exc))]

        value = raw_value * ratio_b / ratio_a
        progress: Optional[float] = None
        limit_expr = str(getattr(slot, "custom_limit_expr", "") or "").strip()
        if limit_expr:
            try:
                limit_value = _extract_number(body_text, limit_expr) * ratio_b / ratio_a
                if limit_value > 0:
                    progress = min(max(value / limit_value, 0.0), 1.0)
            except ValueError as exc:
                logger.warning("[meal_card] 自定义平台上限表达式取值失败，进度条将不显示：%s", exc)

        if value_type == "usage" and progress is not None:
            metrics = [
                MetricValue(
                    id="value",
                    name="用量",
                    kind=KIND_PERCENT,
                    value=progress * 100.0,
                    progress=progress,
                )
            ]
        else:
            metrics = [
                MetricValue(
                    id="value",
                    name="额度" if value_type == "quota" else "数值",
                    kind=KIND_COUNT,
                    value=value,
                    unit=unit,
                    progress=progress,
                )
            ]

        metrics.append(
            MetricValue(
                id="raw",
                name="原始响应",
                kind=KIND_TEXT,
                value=None,
                display=_truncate(_masked_body_text(body_text)),
                ok=True,
            )
        )
        return metrics

    @staticmethod
    def _build_url(slot: Any) -> str:
        """拼出完整请求地址。"""

        base_url = str(getattr(slot, "custom_base_url", "") or "").strip().rstrip("/")
        path = str(getattr(slot, "custom_path", "") or "").strip()
        if path.lower().startswith(("http://", "https://")):
            return path
        if not path:
            return base_url
        return f"{base_url}{path}" if path.startswith("/") else f"{base_url}/{path}"

    @staticmethod
    def _build_headers(slot: Any) -> Dict[str, str]:
        """按鉴权方式与附加请求头生成请求头。"""

        headers: Dict[str, str] = {}
        mode = str(getattr(slot, "custom_auth_mode", "none") or "none").strip().lower()
        value = str(getattr(slot, "custom_auth_value", "") or "").strip()

        if mode == "bearer" and value:
            headers["Authorization"] = f"Bearer {value}"
        elif mode == "header" and value and "=" in value:
            name, header_value = value.split("=", 1)
            if name.strip():
                headers[name.strip()] = header_value.strip()
        elif mode == "basic" and value:
            import base64

            token = base64.b64encode(value.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"

        headers.update(_parse_pairs(list(getattr(slot, "custom_headers", []) or [])))
        return headers

    async def _request(self, slot: Any, url: str, headers: Dict[str, str], *, timeout: float, retries: int) -> Any:
        """带重试地发起请求；成功返回响应体文本，失败返回失败指标。"""

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 宿主已依赖 httpx
            return _failure("raw", "查询失败", f"缺少 httpx 依赖：{exc}")

        method = str(getattr(slot, "custom_method", "GET") or "GET").strip().upper()
        attempts = max(int(retries), 0) + 1
        last_error = "未发起任何请求"

        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            for attempt in range(1, attempts + 1):
                try:
                    if method == "POST":
                        response = await client.post(url, headers=headers)
                    else:
                        response = await client.get(url, headers=headers)
                except Exception as exc:
                    last_error = f"网络异常：{exc}"
                else:
                    if response.status_code < 400:
                        return response.text
                    detail = _truncate(response.text)
                    if response.status_code >= 500 or response.status_code == 429:
                        last_error = f"HTTP {response.status_code}：{detail}"
                    else:
                        logger.error("[meal_card] 自定义平台查询失败：HTTP %s：%s", response.status_code, detail)
                        return _failure("raw", "查询失败", f"HTTP {response.status_code}：{detail}")

                if attempt < attempts:
                    logger.warning(
                        "[meal_card] 自定义平台请求失败（第 %d/%d 次）：%s；稍后重试",
                        attempt,
                        attempts,
                        last_error,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 5.0))

        logger.error("[meal_card] 自定义平台查询失败：%s", last_error)
        return _failure("raw", "查询失败", f"请求失败（共尝试 {attempts} 次）：{last_error}")
