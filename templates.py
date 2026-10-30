"""图片 HTML 模板：内置模板 + 用户新增模板（无需改代码）的加载与占位符填充。

模板占位符使用**双花括号** ``{{键}}``（避免与 HTML/CSS 的花括号冲突），键名与文本模板一致，
另有 ``{{items_html}}``（多行查询项；多平台时按平台分组为 ``.platform-group`` 块）、
``{{title}}``（报告标题，取宿主机器人昵称）、``{{footer}}``（页脚，可在渲染配置里开关与自定义文案，
关闭或留空时整行移除）、``{{font_links}}``（外部字体 link 整段；系统字体模式下为空）、
``{{font_css}}`` / ``{{font_static}}``（字体服务组的原始基地址，保留给自定义模板）。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import html
import logging
import re

from .config import MealCardConfig
from .platforms import MetricValue
from .text_report import build_placeholders, group_by_platform, has_multiple_platforms

logger = logging.getLogger(__name__)

_TEMPLATE_TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*\}\}")
_URL_ALLOWED = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")
"""字体服务基地址白名单：必须是 http(s) 且只含 URL 合法字符（挡掉引号、尖括号等注入字符）。"""
_BUILTIN_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_USER_TEMPLATE_DIR_NAME = "templates"

DEFAULT_TITLE = "麦麦饭卡"
"""读不到宿主机器人昵称时的回退标题。"""

_TITLE_SUFFIX = "的饭卡"
"""标题后缀：最终标题为「{机器人昵称}的饭卡」。"""

DEFAULT_FOOTER = "由麦麦饭卡插件渲染"
"""页脚默认文案（宿主配置未提供或读取失败时使用）。"""

_FOOTER_HTML = '    <div class="footer">{{footer}}</div>\n'
"""页脚行占位文本：开启页脚时替换为模板里的 ``{{footer}}``，关闭时移除整行避免留白。"""


@dataclass(frozen=True)
class FontService:
    """字体服务组。"""

    name: str
    css_base: str
    static_base: str


SYSTEM_FONT_SERVICE = FontService(name="系统字体", css_base="", static_base="")
"""系统字体配置：不加载任何外部字体，由模板里的系统中文字体栈渲染。"""


def _safe_base_url(raw: Any) -> str:
    """校验并规范化字体服务基地址；非法时返回空串。

    字体地址会被直接拼进渲染用的 HTML（``<link href="…">``），因此除了要求 ``http(s)://``
    之外，还限制为 URL 合法字符，避免引号、尖括号等字符注入标记。
    """

    text = str(raw or "").strip().rstrip("/")
    if not text:
        return ""
    if not _URL_ALLOWED.fullmatch(text):
        logger.warning(
            "[meal_card] 字体服务地址非法（必须是 http(s) 且不含引号/尖括号等字符），已忽略：%s",
            raw,
        )
        return ""
    return text


def parse_font_services(items: Sequence[str]) -> List[FontService]:
    """解析字体服务组条目（``name=;css=;static=``）。"""

    services: List[FontService] = []
    for raw in items:
        fields: Dict[str, str] = {}
        for chunk in str(raw or "").split(";"):
            item = chunk.strip()
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            fields[name.strip().lower()] = value.strip()
        css_base = _safe_base_url(fields.get("css", ""))
        if not css_base:
            logger.warning("[meal_card] 字体服务组条目非法（缺少合法的 http(s) css 地址），已跳过：%s", raw)
            continue
        static_base = _safe_base_url(fields.get("static", ""))
        services.append(
            FontService(
                name=fields.get("name", "") or "默认",
                css_base=css_base,
                static_base=static_base,
            )
        )
    return services


def _build_font_links(css_base: str, static_base: str) -> str:
    """生成外部字体的 ``<link>`` 片段；没有 css 基地址时返回空串。"""

    if not css_base:
        return ""
    parts: List[str] = []
    if static_base:
        parts.append(f'  <link rel="preconnect" href="{static_base}" />')
    parts.append(
        f'  <link href="{css_base}/css2?family=Noto+Sans+SC:wght@400;700;900&display=swap" rel="stylesheet" />'
    )
    return "\n".join(parts)


def _user_template_dir(ctx: Any) -> Optional[Path]:
    """返回用户模板目录（插件持久化目录下的 templates）。"""

    paths = getattr(ctx, "paths", None)
    data_dir = getattr(paths, "data_dir", None) if paths is not None else None
    if data_dir is None:
        return None
    return Path(data_dir) / _USER_TEMPLATE_DIR_NAME


def list_templates(ctx: Any) -> Dict[str, Path]:
    """列出可用模板：内置目录 + 用户目录（同名以用户目录优先）。"""

    templates: Dict[str, Path] = {}
    for directory in (_BUILTIN_TEMPLATE_DIR, _user_template_dir(ctx)):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.html")):
            templates[path.stem] = path
    return templates


def resolve_template_path(ctx: Any, template_name: str) -> Optional[Path]:
    """解析模板文件路径。"""

    name = str(template_name or "").strip()
    if not name:
        return None
    return list_templates(ctx).get(name)


async def resolve_report_title(ctx: Any) -> str:
    """解析报告标题为「{宿主机器人昵称}的饭卡」。

    读取宿主配置 ``bot.nickname``（机器人昵称）；读取失败或昵称为空时回退 :data:`DEFAULT_TITLE`。
    """

    nickname = ""
    try:
        raw = await ctx.config.get("bot.nickname")
    except Exception as exc:  # noqa: BLE001 - 读不到昵称不能影响出图
        logger.warning("[meal_card] 读取宿主机器人昵称失败，标题回退默认值：%s", exc)
        return DEFAULT_TITLE
    if isinstance(raw, dict):  # 防御：包装层若未解包 envelope
        if raw.get("success") is False:
            return DEFAULT_TITLE
        raw = raw.get("value")
    nickname = str(raw or "").strip()
    return f"{nickname}{_TITLE_SUFFIX}" if nickname else DEFAULT_TITLE


def _build_items_html(visible: Sequence[MetricValue]) -> str:
    """构造图片里的指标区块。

    **多平台时按平台分组**：每组是一个 ``.platform-group`` 块，带平台名标题
    （``.platform-title``）与左侧分隔条（由模板 CSS 提供），避免不同平台的数据混在一起；
    单平台时保持扁平的 ``<br>`` 列表（观感与旧版一致）。
    """

    if not has_multiple_platforms(visible):
        return "<br>".join(html.escape(f"{metric.name}：{metric.display}") for metric in visible)

    blocks: List[str] = []
    for name, metrics in group_by_platform(visible):
        lines = "<br>".join(html.escape(f"{metric.name}：{metric.display}") for metric in metrics)
        title = f'<div class="platform-title">{html.escape(name)}</div>' if name else ""
        blocks.append(f'<div class="platform-group">{title}<div class="platform-items">{lines}</div></div>')
    return "".join(blocks)


def build_html(
    ctx: Any,
    template_name: str,
    results: Sequence[MetricValue],
    config: MealCardConfig,
    font_service: FontService,
    platform_name: str = "",
    title: str = DEFAULT_TITLE,
) -> str:
    """把查询结果填充进 HTML 模板；模板缺失时回退 simple。

    **图片不展示「原始响应」**：成功的 ``raw`` 指标（原始响应摘要）会被剔除，
    只保留额度 / 用量等正常指标；失败指标照常保留，以便在图上看到失败原因。

    Raises:
        FileNotFoundError: 连内置 simple 模板都找不到时抛出。
    """

    templates = list_templates(ctx)
    path = templates.get(str(template_name or "").strip())
    if path is None:
        if template_name:
            logger.error(
                "[meal_card] 图片模板不存在：%s（可用：%s），已回退 simple",
                template_name,
                "、".join(sorted(templates)) or "无",
            )
        path = templates.get("simple")
    if path is None:
        raise FileNotFoundError("未找到任何图片模板（缺少内置 templates/simple.html）")

    template_text = path.read_text(encoding="utf-8")

    # 页脚：关闭或文案留空时，把模板里的整行页脚删掉，避免留下空白（而不是只清空文本）。
    show_footer = bool(getattr(config.render, "show_footer", True))
    footer_text = str(getattr(config.render, "footer_text", DEFAULT_FOOTER) or "").strip()
    if not (show_footer and footer_text):
        template_text = template_text.replace(_FOOTER_HTML, "")

    visible = [metric for metric in results if not (metric.id == "raw" and metric.ok)]
    if not visible:
        # 若剔除后什么都不剩（例如响应无法解析、只有原始响应），则保留原始结果，避免图上一片空白。
        visible = list(results)
    values: Dict[str, str] = build_placeholders(visible, config, platform_name)
    items_html = _build_items_html(visible)
    escaped: Dict[str, str] = {key: html.escape(value) for key, value in values.items()}
    escaped["items_html"] = items_html
    escaped["title"] = html.escape(str(title or DEFAULT_TITLE))
    escaped["footer"] = html.escape(footer_text)
    css_base = _safe_base_url(font_service.css_base)
    static_base = _safe_base_url(font_service.static_base)
    escaped["font_css"] = css_base
    escaped["font_static"] = static_base
    escaped["font_links"] = _build_font_links(css_base, static_base)

    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in escaped:
            return escaped[key]
        logger.warning("[meal_card] 图片模板使用了未知占位符：{{%s}}", key)
        return ""

    return _TEMPLATE_TOKEN.sub(_replace, template_text)
