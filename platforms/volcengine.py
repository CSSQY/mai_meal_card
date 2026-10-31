"""火山方舟 Agent Plan（AFP）适配器。

接口（官方文档核实：https://docs.volcengine.com/docs/ark/get-afp-usage-api）：
    POST https://ark.cn-beijing.volcengineapi.com/?Action=GetAFPUsage&Version=2024-01-01
    Content-Type: application/json
    Authorization: HMAC-SHA256 ...（火山引擎 Access Key 签名）

该接口仅支持 Access Key 鉴权（Access Key ID + Secret Access Key），不支持方舟 API Key。
返回 5 小时 / 近一天 / 近一周 / 近一月四个滚动窗口的 AFP 配额与已用量；
Quota / Used 单位为 AFP，ResetTime / SubscribeTime 为 epoch 毫秒。

防御式解析：字段缺失只跳过对应指标、数值解析失败保留截断后的原始响应，
绝不抛异常、绝不静默填 0。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import asyncio
import hashlib
import hmac
import json
import logging
import time

from .base import KIND_COUNT, KIND_DURATION, KIND_PERCENT, KIND_TEXT, MetricValue, mask_sensitive

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volcengineapi.com"
_REGION = "cn-beijing"
_SERVICE = "ark"
_ACTION = "GetAFPUsage"
_VERSION = "2024-01-01"
_CONTENT_TYPE = "application/json"
_SIGNED_HEADERS = "content-type;host;x-content-sha256;x-date"
_AFP_UNIT = "AFP"
_ERROR_BODY_LIMIT = 300
_RAW_DISPLAY_LIMIT = 300

# (响应字段, 指标 id 前缀, 中文名)
_WINDOWS: Tuple[Tuple[str, str, str], ...] = (
    ("AFPFiveHour", "afp_5h", "5h窗口"),
    ("AFPDaily", "afp_daily", "近一天窗口"),
    ("AFPWeekly", "afp_weekly", "近一周窗口"),
    ("AFPMonthly", "afp_monthly", "近一月窗口"),
)


def _to_number(raw: Any) -> Optional[float]:
    """把字符串/数字字段转为数值；无法解析时返回 None。"""

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


def _canonical_host(host: str) -> str:
    """签名使用的 host：按火山引擎 SDK 约定去掉默认端口 80/443。"""

    for suffix in (":80", ":443"):
        if host.endswith(suffix):
            return host[: -len(suffix)]
    return host


def _uri_encode(value: Any) -> str:
    """按 RFC3986 编码查询参数（空格编码为 %20）。"""

    return quote(str(value), safe="-_.~")


def _canonical_query(params: Dict[str, str]) -> str:
    """构造火山引擎签名使用的规范化查询字符串。"""

    pairs = sorted((_uri_encode(name), _uri_encode(value)) for name, value in params.items())
    return "&".join(f"{name}={value}" for name, value in pairs)


def _sha256_hex(data: bytes) -> str:
    """计算 SHA256 并返回小写十六进制。"""

    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    """计算 HMAC-SHA256 二进制摘要。"""

    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    """按火山引擎 V4 规则派生签名密钥。"""

    key_date = _hmac_sha256(secret_key.encode("utf-8"), date)
    key_region = _hmac_sha256(key_date, region)
    key_service = _hmac_sha256(key_region, service)
    return _hmac_sha256(key_service, "request")


def build_request(
    access_key: str,
    secret_key: str,
    base_url: str,
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, Dict[str, str], str]:
    """构造带 Access Key 签名的请求。

    Returns:
        Tuple[str, Dict[str, str], str]: ``(完整 URL, 请求头, 请求体)``。

    Raises:
        ValueError: 基地址缺少 host 或时间格式异常时抛出。
    """

    normalized_base = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized_base)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"火山方舟基地址非法：{base_url!r}")
    host = parsed.netloc
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"

    current = now or datetime.now(timezone.utc)
    x_date = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    query = _canonical_query({"Action": _ACTION, "Version": _VERSION})
    body = "{}"
    payload_hash = _sha256_hex(body.encode("utf-8"))

    canonical_headers = (
        f"content-type:{_CONTENT_TYPE}\n"
        f"host:{_canonical_host(host)}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = (
        f"POST\n{path}\n{query}\n{canonical_headers}\n{_SIGNED_HEADERS}\n{payload_hash}"
    )
    credential_scope = f"{short_date}/{_REGION}/{_SERVICE}/request"
    string_to_sign = (
        f"HMAC-SHA256\n{x_date}\n{credential_scope}\n{_sha256_hex(canonical_request.encode('utf-8'))}"
    )
    signature = _hmac_sha256(_signing_key(secret_key, short_date, _REGION, _SERVICE), string_to_sign).hex()

    url = f"{parsed.scheme}://{host}{path}?{query}"
    headers = {
        "Content-Type": _CONTENT_TYPE,
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={_SIGNED_HEADERS}, Signature={signature}"
        ),
    }
    return url, headers, body


class VolcengineAdapter:
    """火山方舟 Agent Plan 适配器（AFP 额度用量查询）。"""

    id = "volcengine"
    display_name = "火山方舟 Agent Plan"

    def is_configured(self, slot: Any) -> Tuple[bool, str]:
        """检查是否已填写 Access Key ID 与 Secret Access Key。"""

        access_key = str(getattr(slot, "access_key", "") or "").strip()
        secret_key = str(getattr(slot, "secret_key", "") or "").strip()
        if not access_key or not secret_key:
            return False, "未填写火山方舟 Access Key ID / Secret Access Key"
        return True, ""

    def metric_ids(self) -> List[str]:
        """返回本平台提供的指标 id 列表。"""

        ids: List[str] = []
        for _field, prefix, _label in _WINDOWS:
            ids.extend([f"{prefix}_remaining", f"{prefix}_used", f"{prefix}_reset"])
        ids.extend(["plan_type", "raw"])
        return ids

    def describe(self, slot: Any) -> str:
        """返回该槽位的中文描述（基地址被覆盖时带上地址）。"""

        override = str(getattr(slot, "base_url", "") or "").strip()
        if override:
            return f"{self.display_name}（{override.rstrip('/')}）"
        return self.display_name

    def base_url(self, slot: Any) -> str:
        """解析基地址：优先槽位覆盖，否则用官方管控面地址。"""

        override = str(getattr(slot, "base_url", "") or "").strip()
        return override.rstrip("/") or _DEFAULT_BASE_URL

    async def fetch(self, slot: Any, *, timeout: float, retries: int) -> List[MetricValue]:
        """查询 AFP 用量并解析；本函数不抛异常。"""

        configured, reason = self.is_configured(slot)
        if not configured:
            return [_failure("config", "未配置", reason)]

        access_key = str(getattr(slot, "access_key", "") or "").strip()
        secret_key = str(getattr(slot, "secret_key", "") or "").strip()

        try:
            url, headers, body = build_request(access_key, secret_key, self.base_url(slot))
        except Exception as exc:
            logger.error("[meal_card] 火山方舟请求构造失败：%s", exc)
            return [_failure("raw", "请求构造失败", str(exc))]

        try:
            body_text = await self._request(url, headers, body, timeout=timeout, retries=retries)
        except Exception as exc:
            logger.error("[meal_card] 火山方舟查询失败：%s", exc)
            return [_failure("raw", "查询失败", str(exc))]

        try:
            payload = json.loads(body_text)
        except Exception as exc:
            logger.error("[meal_card] 火山方舟响应不是合法 JSON：%s；原文：%s", exc, _truncate(body_text))
            return [_failure("raw", "响应解析失败", f"响应不是合法 JSON：{exc}")]

        if not isinstance(payload, dict):
            return [_failure("raw", "响应解析失败", f"响应结构异常：{type(payload).__name__}")]

        metrics = self._build_metrics(payload)
        if not metrics:
            logger.warning(
                "[meal_card] 火山方舟响应中没有可用的 AFP 用量字段，原文：%s",
                _truncate(json.dumps(payload, ensure_ascii=False)),
            )
            return [_failure("raw", "响应解析失败", "响应中缺少 Result 或 AFP 用量字段")]
        return metrics

    @staticmethod
    def _build_metrics(payload: Dict[str, Any]) -> List[MetricValue]:
        """把响应体解析为指标列表（跳过缺失/非法字段）。"""

        response_meta = payload.get("ResponseMetadata")
        if isinstance(response_meta, dict):
            error = response_meta.get("Error")
            if isinstance(error, dict):
                code = str(error.get("Code") or error.get("CodeN") or "").strip()
                message = str(error.get("Message") or "").strip()
                return [_failure("raw", "接口返回错误", f"{code} {message}".strip())]

        result = payload.get("Result")
        if not isinstance(result, dict):
            return []

        metrics: List[MetricValue] = []
        plan_type = str(result.get("PlanType") or "").strip()
        if plan_type:
            metrics.append(
                MetricValue(
                    id="plan_type",
                    name="套餐档位",
                    kind=KIND_TEXT,
                    value=None,
                    display=plan_type,
                    ok=True,
                )
            )

        for field, prefix, label in _WINDOWS:
            entry = result.get(field)
            if not isinstance(entry, dict):
                continue
            quota = _to_number(entry.get("Quota"))
            used = _to_number(entry.get("Used"))

            if quota is not None and used is not None and quota > 0:
                remaining = max(0.0, min(100.0, (quota - used) / quota * 100.0))
                metrics.append(
                    MetricValue(
                        id=f"{prefix}_remaining",
                        name=f"{label}剩余",
                        kind=KIND_PERCENT,
                        value=remaining,
                        progress=remaining / 100.0,
                    )
                )

            if used is not None:
                metrics.append(
                    MetricValue(
                        id=f"{prefix}_used",
                        name=f"{label}已用",
                        kind=KIND_COUNT,
                        value=used,
                        unit=_AFP_UNIT,
                    )
                )

            reset_ms = _to_number(entry.get("ResetTime"))
            if reset_ms is not None:
                remaining_ms = max(reset_ms - time.time() * 1000, 0.0)
                metrics.append(
                    MetricValue(
                        id=f"{prefix}_reset",
                        name=f"{label}重置倒计时",
                        kind=KIND_DURATION,
                        value=remaining_ms,
                    )
                )

        if not metrics:
            return []
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

    async def _request(
        self, url: str, headers: Dict[str, str], body: str, *, timeout: float, retries: int
    ) -> str:
        """带重试地发起一次 POST 请求，返回响应体文本。"""

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 宿主已依赖 httpx
            raise RuntimeError(f"缺少 httpx 依赖，无法发起查询：{exc}") from exc

        attempts = max(int(retries), 0) + 1
        last_error = "未发起任何请求"

        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.post(url, headers=headers, content=body)
                except Exception as exc:
                    last_error = f"网络异常：{exc}"
                else:
                    if response.status_code < 400:
                        return response.text
                    detail = _truncate(response.text)
                    if response.status_code in {401, 403}:
                        raise RuntimeError(
                            "401/403 鉴权失败：请检查火山方舟 Access Key ID / Secret Access Key，"
                            "并确认账号已开通 Agent Plan 套餐"
                        )
                    if response.status_code >= 500 or response.status_code == 429:
                        last_error = f"HTTP {response.status_code}：{detail}"
                    else:
                        raise RuntimeError(f"HTTP {response.status_code}：{detail}")

                if attempt < attempts:
                    logger.warning(
                        "[meal_card] 火山方舟请求失败（第 %d/%d 次）：%s；稍后重试",
                        attempt,
                        attempts,
                        last_error,
                    )
                    await asyncio.sleep(min(0.5 * attempt, 5.0))

        raise RuntimeError(f"请求失败（共尝试 {attempts} 次）：{last_error}")
