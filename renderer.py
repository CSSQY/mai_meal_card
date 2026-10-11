"""图片渲染管线：字体服务组轮换、两轮超时、格式转换与失败回退。"""

from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

import base64
import logging
import time

from .config import MealCardConfig
from .platforms import MetricValue
from .templates import DEFAULT_TITLE, FontService, build_html, parse_font_services, resolve_report_title

logger = logging.getLogger(__name__)

_TOTAL_RENDER_BUDGET_MS = 45000
_RENDER_SELECTOR = "#meal-card"


class RenderFailed(RuntimeError):
    """渲染整体失败。"""


async def render_image(
    ctx: Any,
    results: Sequence[MetricValue],
    config: MealCardConfig,
    platform_name: str = "",
) -> str:
    """把查询结果渲染为图片 base64（PNG 或 JPEG）。

    Raises:
        RenderFailed: 全部尝试都失败时抛出。
    """

    services = parse_font_services(config.render.font_services)
    if not services:
        raise RenderFailed("字体服务组为空或全部非法，无法渲染（请检查 render.font_services 配置）")

    title = await resolve_report_title(ctx)

    render_config = config.render
    rounds: List[Tuple[int, int, int]] = [
        (render_config.first_round_timeout_ms, render_config.viewport_width, render_config.viewport_height),
        (
            render_config.second_round_timeout_ms,
            render_config.fallback_viewport_width,
            render_config.fallback_viewport_height,
        ),
    ]
    deadline = time.monotonic() + _TOTAL_RENDER_BUDGET_MS / 1000
    last_error = "未执行任何渲染尝试"

    for round_index, (timeout_ms, viewport_width, viewport_height) in enumerate(rounds, start=1):
        for service in services:
            if time.monotonic() > deadline:
                raise RenderFailed(f"渲染总预算（{_TOTAL_RENDER_BUDGET_MS}ms）已耗尽，最后一次错误：{last_error}")
            image_base64, error = await _render_once(
                ctx,
                results=results,
                config=config,
                service=service,
                round_index=round_index,
                timeout_ms=timeout_ms,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                platform_name=platform_name,
                title=title,
            )
            if image_base64:
                return image_base64
            last_error = error

    raise RenderFailed(f"渲染失败（已尝试 {len(rounds)} 轮 × {len(services)} 个字体服务）：{last_error}")


async def _render_once(
    ctx: Any,
    *,
    results: Sequence[MetricValue],
    config: MealCardConfig,
    service: FontService,
    round_index: int,
    timeout_ms: int,
    viewport_width: int,
    viewport_height: int,
    platform_name: str = "",
    title: str = DEFAULT_TITLE,
) -> Tuple[Optional[str], str]:
    """执行一次渲染尝试并转换图片格式。"""

    try:
        html_text = build_html(ctx, config.render.template_name, results, config, service, platform_name, title)
    except Exception as exc:
        logger.error("[meal_card] 构造图片 HTML 失败：%s", exc)
        return None, str(exc)

    render_kwargs: Dict[str, Any] = {
        "selector": _RENDER_SELECTOR,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "device_scale_factor": config.render.resolution_scale,
        "full_page": False,
        "wait_until": "load",
        "allow_network": config.render.allow_network,
    }
    try:
        result = await ctx.render.html2png(html_text, render_timeout_ms=timeout_ms, **render_kwargs)
    except TypeError as exc:
        if "render_timeout_ms" not in str(exc):
            logger.warning("[meal_card] 渲染失败（第%d轮 / 字体服务 %s）：%s", round_index, service.name, exc)
            return None, str(exc)
        logger.info("[meal_card] 当前插件 SDK 使用旧参数名 timeout_ms，已自动按旧版重试")
        try:
            result = await ctx.render.html2png(html_text, timeout_ms=timeout_ms, **render_kwargs)
        except Exception as inner_exc:
            logger.warning("[meal_card] 渲染失败（第%d轮 / 字体服务 %s）：%s", round_index, service.name, inner_exc)
            return None, str(inner_exc)
    except Exception as exc:
        logger.warning("[meal_card] 渲染失败（第%d轮 / 字体服务 %s）：%s", round_index, service.name, exc)
        return None, str(exc)

    image_base64 = _extract_image_base64(result)
    if not image_base64:
        logger.warning("[meal_card] 渲染返回缺少 image_base64（第%d轮 / 字体服务 %s）", round_index, service.name)
        return None, "渲染返回缺少 image_base64"

    try:
        converted = _convert_image_format(image_base64, config)
    except Exception as exc:
        logger.error("[meal_card] 图片格式转换失败（目标格式 %s）：%s", config.render.image_format, exc)
        return None, f"图片格式转换失败：{exc}"

    logger.info(
        "[meal_card] 图片渲染成功：模板=%s，字体服务=%s，第%d轮，格式=%s",
        config.render.template_name,
        service.name,
        round_index,
        config.render.image_format,
    )
    return converted, ""


def _extract_image_base64(payload: Any) -> str:
    """从渲染返回值中提取图片 base64。"""

    if isinstance(payload, dict):
        image_base64 = str(payload.get("image_base64") or "").strip()
        if image_base64:
            return image_base64
        inner = payload.get("result")
        if isinstance(inner, dict):
            return str(inner.get("image_base64") or "").strip()
    return ""


def _convert_image_format(image_base64: str, config: MealCardConfig) -> str:
    """按配置转换图片格式（PNG 原样返回，JPEG 使用 Pillow 转码）。"""

    if config.render.image_format != "jpeg":
        return image_base64

    from PIL import Image

    try:
        png_bytes = base64.b64decode(image_base64)
    except Exception as exc:
        raise ValueError(f"图片 base64 解码失败：{exc}") from exc

    buffer = BytesIO()
    with Image.open(BytesIO(png_bytes)) as image:
        image.convert("RGB").save(buffer, format="JPEG", quality=int(config.render.jpeg_quality))
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


async def build_image(
    ctx: Any,
    results: Sequence[MetricValue],
    config: MealCardConfig,
    platform_name: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """渲染图片并返回 ``(图片 base64, 失败原因)``；本函数不会抛出异常。"""

    try:
        image_base64 = await render_image(ctx, results, config, platform_name)
    except RenderFailed as exc:
        return None, str(exc)
    if not image_base64:
        return None, "渲染未返回图片数据"
    return image_base64, None
