"""文本模板渲染：把指标结果填充进占位符模板。

占位符（与图片模板共用同一套名称）：
- ``{date}`` 生成时间、``{count}`` 指标数量、``{items}`` 全部指标多行区块、
  ``{unavailable}`` 失败清单、``{platforms}`` 平台清单（``{platform}`` 为其别名）；
- 每个指标 ``<平台标识>.<指标id>`` 对应：``{id}`` 展示值、``{id}_raw`` 纯数值、
  ``{id}_name`` 显示名、``{id}_progress`` 进度百分比、``{id}_error`` 错误信息；
- 当只有一个平台时，额外提供不带前缀的简写 ``{指标id}``。
"""

from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import logging
import re

from .config import DEFAULT_TEXT_TEMPLATE, MealCardConfig
from .platforms import MetricValue

logger = logging.getLogger(__name__)

_UNKNOWN_WARNED: set = set()


class _SafeDict(dict):
    """缺失占位符时置空并记录一次 warning。"""

    def __missing__(self, key: str) -> str:
        if key not in _UNKNOWN_WARNED:
            _UNKNOWN_WARNED.add(key)
            logger.warning("[meal_card] 文本模板使用了未知占位符：{%s}", key)
        return ""


def _platform_names(results: Sequence[MetricValue]) -> List[str]:
    """按出现顺序返回去重后的平台显示名清单。"""

    names: List[str] = []
    for metric in results:
        if metric.platform_name and metric.platform_name not in names:
            names.append(metric.platform_name)
    return names


def has_multiple_platforms(results: Sequence[MetricValue]) -> bool:
    """判断结果是否来自多个平台（用于决定是否按平台分组展示）。"""

    return len({metric.platform_key for metric in results if metric.platform_key}) > 1


def group_by_platform(results: Sequence[MetricValue]) -> List[Tuple[str, List[MetricValue]]]:
    """按平台把指标分组（保持首次出现顺序）。

    Returns:
        List[Tuple[str, List[MetricValue]]]: ``(平台显示名, 该平台的指标)`` 列表；
        归属不到平台的指标会归入名称为空的组。
    """

    groups: List[Tuple[str, List[MetricValue]]] = []
    positions: Dict[str, int] = {}
    for metric in results:
        key = metric.platform_key or metric.platform_name or ""
        if key not in positions:
            positions[key] = len(groups)
            groups.append((metric.platform_name or "", []))
        groups[positions[key]][1].append(metric)
    return groups


def build_placeholders(
    results: Sequence[MetricValue],
    config: MealCardConfig,
    platform_name: str = "",
) -> Dict[str, str]:
    """构造占位符字典。"""

    del config
    placeholders: _SafeDict = _SafeDict()
    placeholders["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    placeholders["count"] = str(len(results))

    names = _platform_names(results)
    if not names and platform_name:
        names = [platform_name]
    joined = "、".join(names)
    placeholders["platforms"] = joined
    placeholders["platform"] = names[0] if names else platform_name

    groups = group_by_platform(results)
    multiple = has_multiple_platforms(results)
    item_lines: List[str] = []
    failed_lines: List[str] = []
    for name, metrics in groups:
        header = f"【{name}】" if multiple and name else ""
        if header:
            if item_lines:
                item_lines.append("")
            item_lines.append(header)
        for metric in metrics:
            item_lines.append(f"{metric.name}：{metric.display}" if metric.ok else f"{metric.name}：查询失败")
    for name, metrics in groups:
        failures = [metric for metric in metrics if not metric.ok]
        if not failures:
            continue
        if multiple and name:
            failed_lines.append(f"【{name}】")
        failed_lines.extend(f"{metric.name}：{metric.error}" for metric in failures)

    placeholders["items"] = "\n".join(item_lines)
    placeholders["unavailable"] = "\n".join(failed_lines)

    single_platform = len({metric.platform_key for metric in results if metric.platform_key}) <= 1
    for metric in results:
        keys = [metric.qualified_id]
        if single_platform and metric.id not in keys:
            keys.append(metric.id)
        for key in keys:
            placeholders[key] = metric.display
            placeholders[f"{key}_name"] = metric.name
            placeholders[f"{key}_error"] = metric.error
            placeholders[f"{key}_raw"] = "" if metric.value is None else f"{metric.value:g}"
            placeholders[f"{key}_progress"] = "" if metric.progress is None else f"{metric.progress * 100:.1f}%"
    return placeholders


def _compress_blank_lines(text: str) -> str:
    """压缩连续空行并去掉首尾空白。"""

    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_text_report(
    results: Sequence[MetricValue],
    config: MealCardConfig,
    platform_name: str = "",
    template: Optional[str] = None,
) -> str:
    """渲染文本报告；模板语法错误时回退内置默认模板。"""

    raw_template = template if template is not None and template.strip() else config.text_template.template
    placeholders = build_placeholders(results, config, platform_name)
    try:
        rendered = raw_template.format_map(placeholders)
    except Exception as exc:
        logger.error("[meal_card] 文本模板语法错误，已回退默认模板：%s", exc)
        rendered = DEFAULT_TEXT_TEMPLATE.format_map(placeholders)
    return _compress_blank_lines(rendered)


def build_data_block(results: Sequence[MetricValue], platform_name: str = "") -> str:
    """构造给 LLM 的纯数据块（多平台时按平台分组）。"""

    names = _platform_names(results)
    if not names and platform_name:
        names = [platform_name]
    multiple = has_multiple_platforms(results)
    lines: List[str] = []
    if names:
        lines.append(f"平台：{'、'.join(names)}")
    for name, metrics in group_by_platform(results):
        if multiple and name:
            lines.append(f"【{name}】")
        for metric in metrics:
            if metric.ok:
                lines.append(f"- {metric.name}：{metric.display}")
            else:
                lines.append(f"- {metric.name}：查询失败（{metric.error}）")
    return "\n".join(lines)
