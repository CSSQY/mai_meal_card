"""投递：目标会话解析与内容发送。"""

from typing import Any, List, Optional, Sequence, Tuple

import logging

from .config import TargetSpec

logger = logging.getLogger(__name__)

_CHAT_PLATFORM = "qq"
"""投递使用的聊天平台。

本插件只向 QQ 群 / QQ 用户播报（目标格式为 ``群:号码`` / ``用户:号码``），
因此无需在任务里单独配置平台，统一走 QQ。
"""


def _extract_stream_id(payload: Any) -> str:
    """从聊天流相关返回值里提取 stream_id。"""

    if not isinstance(payload, dict):
        return ""
    for key in ("session_id", "stream_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    stream = payload.get("stream")
    if isinstance(stream, dict):
        for key in ("session_id", "stream_id"):
            value = str(stream.get(key) or "").strip()
            if value:
                return value
    return ""


async def resolve_target_stream_id(ctx: Any, target: TargetSpec) -> Optional[str]:
    """解析单个目标的 stream_id，找不到时尝试创建会话。"""

    label = f"{'群' if target.chat_type == 'group' else '用户'} {target.target_id}"
    try:
        if target.chat_type == "group":
            stream = await ctx.chat.get_stream_by_group_id(target.target_id, platform=_CHAT_PLATFORM)
        else:
            stream = await ctx.chat.get_stream_by_user_id(target.target_id, platform=_CHAT_PLATFORM)
        stream_id = _extract_stream_id(stream)
        if stream_id:
            return stream_id

        if target.chat_type == "group":
            opened = await ctx.chat.open_session(platform=_CHAT_PLATFORM, chat_type="group", group_id=target.target_id)
        else:
            opened = await ctx.chat.open_session(platform=_CHAT_PLATFORM, chat_type="private", user_id=target.target_id)
        stream_id = _extract_stream_id(opened)
        if stream_id:
            return stream_id
        logger.warning("[meal_card] %s 无法解析聊天流，已跳过", label)
    except Exception as exc:
        logger.warning("[meal_card] %s 解析失败：%s", label, exc)
    return None


def _extract_send_outcome(payload: Any) -> Tuple[bool, str]:
    """解析宿主发送能力的返回值 ``(是否成功, 失败原因)``。

    新版 SDK 的 ``ctx.send.*`` 默认直接返回布尔值（``return_details=True`` 时返回
    ``{"sent": ..., "message_id": ...}``），旧版 SDK 则可能返回宿主原始结构
    ``{"success": ..., "error": ...}``；这里统一兼容三种形态。
    """

    if isinstance(payload, bool):
        return payload, "" if payload else "宿主未给出失败原因"
    if isinstance(payload, dict):
        if "success" in payload:
            if payload.get("success"):
                return True, ""
            return False, str(payload.get("error") or "宿主未给出失败原因")
        if "sent" in payload:
            if payload.get("sent"):
                return True, ""
            return False, str(payload.get("error") or "宿主未给出失败原因")
    return False, f"宿主发送能力返回结构异常：{type(payload).__name__}"


async def send_content(ctx: Any, stream_id: str, text: str, image_base64: Optional[str] = None) -> bool:
    """向指定聊天流发送内容（先图片后文本）。"""

    sent_any = False
    if image_base64:
        try:
            sent_image, image_error = _extract_send_outcome(await ctx.send.image(image_base64, stream_id))
            if sent_image:
                sent_any = True
            else:
                logger.warning("[meal_card] 图片发送失败：stream_id=%s，%s", stream_id, image_error)
        except Exception as exc:
            logger.warning("[meal_card] 图片发送异常：stream_id=%s，%s", stream_id, exc)

    if text:
        try:
            sent_text, text_error = _extract_send_outcome(await ctx.send.text(text, stream_id))
            if sent_text:
                sent_any = True
            else:
                logger.warning("[meal_card] 文本发送失败：stream_id=%s，%s", stream_id, text_error)
        except Exception as exc:
            logger.warning("[meal_card] 文本发送异常：stream_id=%s，%s", stream_id, exc)
    return sent_any


async def broadcast_content(
    ctx: Any,
    targets: Sequence[TargetSpec],
    text: str,
    image_base64: Optional[str] = None,
) -> Tuple[int, int]:
    """向多个目标发送内容，返回 ``(成功数, 失败数)``。"""

    success_count = 0
    failure_count = 0
    for target in targets:
        stream_id = await resolve_target_stream_id(ctx, target)
        if not stream_id:
            failure_count += 1
            continue
        if await send_content(ctx, stream_id, text, image_base64):
            success_count += 1
        else:
            failure_count += 1
            logger.warning("[meal_card] 播报发送失败：%s:%s", target.chat_type, target.target_id)
    return success_count, failure_count


def describe_targets(targets: Sequence[TargetSpec]) -> List[str]:
    """返回目标清单的中文描述。"""

    return [f"{'群' if t.chat_type == 'group' else '用户'} {t.target_id}" for t in targets]
