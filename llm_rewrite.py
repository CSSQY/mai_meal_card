"""LLM 风格化转述：宿主模型组 或 自定义 [OI] Chat Completions 兼容服务。

两条路径的失败都统一抛 :class:`LlmCallError`，由调用方回退为模板文本。
"""

from typing import Any, Dict, List, Optional, Sequence

import asyncio
import json
import logging

from .config import MealCardConfig
from .platforms import MetricValue
from .text_report import build_data_block

logger = logging.getLogger(__name__)

_CHAT_COMPLETIONS_PATH = "/chat/completions"
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})
_ERROR_BODY_LIMIT = 300


class LlmCallError(RuntimeError):
    """LLM 转述调用失败。"""


class _HttpStatusError(Exception):
    """自定义服务返回非 2xx 状态码。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}：{detail}")
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.status_code >= 500 or self.status_code in _RETRYABLE_STATUS_CODES


async def _read_config_value(ctx: Any, key: str) -> str:
    """读取宿主全局配置里的字符串值。"""

    try:
        result = await ctx.config.get(key)
    except Exception as exc:
        logger.warning("[meal_card] 读取宿主配置 %s 失败：%s", key, exc)
        return ""
    if isinstance(result, dict):
        if result.get("success") is False:
            return ""
        value = result.get("value")
        return str(value or "").strip()
    return str(result or "").strip()


async def build_persona_prompt(ctx: Any, config: MealCardConfig) -> str:
    """按配置拼装人格提示词。"""

    rewrite = config.llm_rewrite
    extra = str(rewrite.persona_extra or "").strip()
    if not rewrite.use_persona:
        return extra

    bot_name = await _read_config_value(ctx, "bot.nickname")
    personality = await _read_config_value(ctx, "personality.personality")
    reply_style = await _read_config_value(ctx, "personality.reply_style")

    template = str(rewrite.persona_template or "").strip()
    if template:
        try:
            persona = template.format(bot_name=bot_name, personality=personality, reply_style=reply_style)
        except Exception as exc:
            logger.error("[meal_card] 人格提示词模板语法错误，已回退内置拼装：%s", exc)
            persona = ""
    else:
        persona = ""

    if not persona:
        lines: List[str] = []
        if bot_name:
            lines.append(f"你是{bot_name}。")
        if personality:
            lines.append(f"你的人格设定：{personality}")
        if reply_style:
            lines.append(f"你的表达风格：{reply_style}")
        persona = "\n".join(lines)

    if extra:
        persona = f"{persona}\n{extra}" if persona else extra
    return persona.strip()


def build_prompt(persona: str, task_prompt: str, data_block: str) -> str:
    """拼装最终提示词：人格 + 任务说明 + 数据。"""

    sections = [section for section in (persona.strip(), task_prompt.strip(), data_block.strip()) if section]
    return "\n\n".join(sections)


async def generate_rewrite(
    ctx: Any,
    config: MealCardConfig,
    results: Sequence[MetricValue],
    platform_name: str = "",
) -> str:
    """调用 LLM 生成转述文本。

    Raises:
        LlmCallError: 配置缺失或调用失败时抛出。
    """

    rewrite = config.llm_rewrite
    persona = await build_persona_prompt(ctx, config)
    prompt = build_prompt(persona, rewrite.llm_prompt, build_data_block(results, platform_name))

    provider = str(rewrite.provider or "host").strip().lower()
    if provider == "custom":
        return await _generate_via_custom(rewrite, prompt)
    return await _generate_via_host(ctx, rewrite, prompt)


async def _generate_via_host(ctx: Any, rewrite: Any, prompt: str) -> str:
    """复用宿主模型组生成文本。"""

    task_name = str(getattr(rewrite, "llm_task_name", "") or "").strip()
    try:
        result = await ctx.llm.generate(prompt=prompt, task_name=task_name)
    except Exception as exc:
        raise LlmCallError(f"宿主模型组调用异常：{exc}") from exc

    if isinstance(result, dict):
        if result.get("success") and result.get("response"):
            return str(result["response"]).strip()
        if result.get("success") is False:
            raise LlmCallError(f"宿主模型组返回失败：{result.get('error') or '未给出原因'}")
    raise LlmCallError(f"宿主模型组返回结构异常：{type(result).__name__}")


async def _generate_via_custom(rewrite: Any, prompt: str) -> str:
    """调用自定义 [OI] Chat Completions 兼容服务生成文本（含重试）。"""

    base_url = str(getattr(rewrite, "api_base_url", "") or "").strip().rstrip("/")
    api_key = str(getattr(rewrite, "api_key", "") or "").strip()
    model = str(getattr(rewrite, "model", "") or "").strip()
    if not base_url:
        raise LlmCallError("未配置自定义服务地址（llm_rewrite.api_base_url）")
    if not model:
        raise LlmCallError("未配置自定义服务模型名（llm_rewrite.model）")

    url = f"{base_url}{_CHAT_COMPLETIONS_PATH}"
    payload: Dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    temperature = getattr(rewrite, "temperature", None)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    payload.update(_parse_extra_body(getattr(rewrite, "extra_body", "")))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout_seconds = float(getattr(rewrite, "timeout_seconds", 60.0) or 60.0)
    retry_interval = max(float(getattr(rewrite, "retry_interval_seconds", 1.0) or 0.0), 0.0)
    attempts = max(int(getattr(rewrite, "max_retries", 0) or 0), 0) + 1

    try:
        import httpx
    except ImportError as exc:
        raise LlmCallError(f"缺少 httpx 依赖，无法调用自定义服务：{exc}") from exc

    last_error = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
        for attempt in range(1, attempts + 1):
            try:
                return await _post_once(client, url, payload, headers)
            except _HttpStatusError as exc:
                last_error = str(exc)
                if not exc.retryable:
                    raise LlmCallError(f"自定义服务返回错误：{exc}") from exc
            except Exception as exc:
                last_error = str(exc)
            if attempt < attempts:
                logger.warning(
                    "[meal_card] 自定义 LLM 调用失败（第 %d/%d 次）：%s；%s 秒后重试",
                    attempt,
                    attempts,
                    last_error,
                    retry_interval,
                )
                if retry_interval > 0:
                    await asyncio.sleep(retry_interval)

    raise LlmCallError(f"自定义 LLM 调用失败（共尝试 {attempts} 次）：{last_error}")


async def _post_once(client: Any, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> str:
    """发起一次请求并解析出文本内容。"""

    response = await client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        raise _HttpStatusError(response.status_code, _summarize_body(response))
    try:
        data = response.json()
    except Exception as exc:
        raise LlmCallError(f"自定义服务返回的不是合法 JSON：{exc}") from exc

    content = _extract_content(data)
    if not content:
        raise LlmCallError("自定义服务返回内容为空或结构不符合 Chat Completions 约定")
    return content


def _summarize_body(response: Any) -> str:
    """截断错误响应体。"""

    try:
        text = str(response.text or "").strip()
    except Exception:
        return "（无法读取响应体）"
    if not text:
        return "（响应体为空）"
    if len(text) <= _ERROR_BODY_LIMIT:
        return text
    return f"{text[:_ERROR_BODY_LIMIT]}…（已截断）"


def _extract_content(data: Any) -> str:
    """从 Chat Completions 响应中提取文本内容。"""

    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""

    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                text = item.get("text") if isinstance(item, dict) else item
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            if parts:
                return "\n".join(parts)

    legacy_text = first.get("text")
    if isinstance(legacy_text, str) and legacy_text.strip():
        return legacy_text.strip()
    return ""


def _parse_extra_body(raw_extra_body: Any) -> Dict[str, Any]:
    """解析「额外请求参数」配置项（JSON 对象文本）。"""

    text = str(raw_extra_body or "").strip()
    if not text:
        return {}
    try:
        parsed: Optional[Any] = json.loads(text)
    except Exception as exc:
        logger.warning("[meal_card] 额外请求参数不是合法 JSON，已忽略：%s", exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("[meal_card] 额外请求参数必须是 JSON 对象，已忽略：%s", type(parsed).__name__)
        return {}
    return parsed
