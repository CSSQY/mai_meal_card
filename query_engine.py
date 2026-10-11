"""查询引擎：调度平台适配器、过滤指标、统一展示格式化。

设计要点：
- 平台差异全部收敛在 ``platforms/`` 包内，本模块只与 :class:`~.platforms.base.MetricValue` 打交道；
- 支持**多个平台槽位**：逐个启用槽位查询，并为每个指标打上平台前缀（``<平台标识>.<指标id>``）；
- 适配器负责「取值 + 命名」，本模块按 ``[display]`` 配置统一格式化展示串；
- 任何失败都以 ``ok=False`` 的指标返回，不抛异常、不静默填 0。
"""

from dataclasses import replace
from typing import Any, List, Sequence

import logging

from .config import MealCardConfig, norm_platform_key, platform_slots
from .platforms import MetricValue, get_platform
from .platforms.base import KIND_COUNT, KIND_DURATION, KIND_PERCENT, KIND_TEXT, format_duration

logger = logging.getLogger(__name__)

_VALID_KIND_FILTERS = {KIND_PERCENT, KIND_DURATION, KIND_COUNT, KIND_TEXT}


def _format_progress_bar(progress: float, width: int) -> str:
    """生成字符进度条。"""

    clamped = min(max(progress, 0.0), 1.0)
    filled = int(round(clamped * width))
    return "█" * filled + "░" * (width - filled)


def format_metric(metric: MetricValue, config: MealCardConfig) -> MetricValue:
    """按 ``[display]`` 配置为指标补充展示串（不修改原对象）。"""

    if not metric.ok:
        return metric

    display_config = config.display
    if metric.kind == KIND_PERCENT and metric.value is not None:
        parts: List[str] = []
        if display_config.show_progress and metric.progress is not None:
            parts.append(f"[{_format_progress_bar(metric.progress, display_config.progress_bar_width)}]")
        if display_config.show_percentage:
            parts.append(f"{metric.value:.1f}%")
        display = " ".join(parts) or f"{metric.value:.1f}%"
        return replace(metric, display=display)

    if metric.kind == KIND_DURATION and metric.value is not None:
        return replace(metric, display=format_duration(metric.value))

    if metric.kind == KIND_COUNT and metric.value is not None:
        unit_suffix = f" {metric.unit}" if metric.unit else ""
        return replace(metric, display=f"{metric.value:.{display_config.quota_decimals}f}{unit_suffix}")

    return metric


def _failure(metric_id: str, name: str, error: str) -> MetricValue:
    """构造一个失败指标。"""

    return MetricValue(id=metric_id, name=name, kind=KIND_TEXT, value=None, display="查询失败", ok=False, error=error)


def available_metric_ids(config: MealCardConfig) -> List[str]:
    """列出当前配置下可查询的指标 id（带平台前缀），用于提示与文档。"""

    ids: List[str] = []
    for index, slot in enumerate(platform_slots(config), start=1):
        if not getattr(slot, "enabled", True):
            continue
        adapter = get_platform(getattr(slot, "adapter", ""))
        if adapter is None:
            continue
        platform_key = norm_platform_key(getattr(slot, "key", ""), f"platform{index}")
        for metric_id in adapter.metric_ids():
            ids.append(f"{platform_key}.{metric_id}")
    return ids


def _slot_display_name(slot: Any, adapter: Any) -> str:
    """解析槽位的显示名：优先「显示名称」，否则用适配器的描述。"""

    custom = str(getattr(slot, "name", "") or "").strip()
    if custom:
        return custom
    try:
        return adapter.describe(slot)
    except Exception as exc:  # pragma: no cover - 适配器 describe 不应抛异常
        logger.warning("[meal_card] 生成平台描述失败：%s", exc)
        return adapter.display_name or adapter.id


def describe_platforms(config: MealCardConfig) -> str:
    """返回当前启用平台的中文描述（多个用「、」连接）。"""

    names: List[str] = []
    for index, slot in enumerate(platform_slots(config), start=1):
        if not getattr(slot, "enabled", True):
            continue
        adapter = get_platform(getattr(slot, "adapter", ""))
        names.append(_slot_display_name(slot, adapter) if adapter is not None else f"平台{index}")
    return "、".join(names)


async def run_all(
    ctx: Any,
    config: MealCardConfig,
    keys: Sequence[str] = (),
    kind_filter: str = "",
) -> List[MetricValue]:
    """执行一次查询（遍历全部启用的平台槽位）。

    Args:
        ctx: 插件运行时上下文（当前适配器不直接使用，保留以便未来平台按需使用）。
        config: 插件配置。
        keys: 需要查询的指标 id（``<平台标识>.<指标id>``）；留空或含 ``*`` 表示全部。
              也支持只写指标 id（如 ``interval_remaining``），此时匹配所有平台下的该指标。
        kind_filter: 仅保留指定类型（``percent`` / ``duration`` / ``count`` / ``text``）；留空不筛选。

    Returns:
        List[MetricValue]: 指标列表；无可用平台或查询失败时返回 ``ok=False`` 的指标。
    """

    del ctx

    slots = [slot for slot in platform_slots(config) if getattr(slot, "enabled", True)]
    if not slots:
        return [_failure("platform", "未配置平台", "没有启用任何平台槽位（请在「平台」页填写）")]

    collected: List[MetricValue] = []
    for index, slot in enumerate(platform_slots(config), start=1):
        if not getattr(slot, "enabled", True):
            continue
        platform_key = norm_platform_key(getattr(slot, "key", ""), f"platform{index}")
        adapter = get_platform(getattr(slot, "adapter", ""))
        if adapter is None:
            collected.append(
                replace(
                    _failure("platform", f"平台{index}", f"未找到平台适配器：{getattr(slot, 'adapter', '')}"),
                    platform_key=platform_key,
                )
            )
            continue

        platform_name = _slot_display_name(slot, adapter)
        configured, reason = adapter.is_configured(slot)
        if not configured:
            collected.append(
                replace(
                    _failure("config", "未配置", reason),
                    platform_key=platform_key,
                    platform_name=platform_name,
                )
            )
            continue

        try:
            metrics = await adapter.fetch(
                slot,
                timeout=float(config.limits.request_timeout_seconds),
                retries=int(config.limits.max_request_retries),
            )
        except Exception as exc:  # 适配器约定不抛异常，此处兜底
            logger.error("[meal_card] 平台 %s 查询异常：%s", platform_key, exc, exc_info=True)
            metrics = [_failure("raw", "查询失败", str(exc))]

        collected.extend(
            replace(metric, platform_key=platform_key, platform_name=platform_name) for metric in metrics
        )

    if not collected:
        return [_failure("platform", "查询失败", "所有平台槽位都没有返回结果")]

    selected = _filter_by_keys(collected, keys)
    if kind_filter in _VALID_KIND_FILTERS:
        selected = [metric for metric in selected if metric.kind == kind_filter]

    return [format_metric(metric, config) for metric in selected]


def _filter_by_keys(metrics: Sequence[MetricValue], keys: Sequence[str]) -> List[MetricValue]:
    """按指标 id 过滤。

    支持两种写法：
    - 完整 id（``minimax.interval_remaining``）：精确匹配某平台的某指标；
    - 仅指标 id（``interval_remaining``）：匹配所有平台下的该指标。
    留空或含 ``*`` 表示全部。
    """

    requested = [str(key or "").strip() for key in keys if str(key or "").strip()]
    if not requested or "*" in requested:
        return list(metrics)

    wanted = set(requested)
    available = {metric.qualified_id for metric in metrics}
    for key in sorted(wanted - available):
        logger.warning("[meal_card] 指标不存在，已跳过：%s", key)
    return [
        metric
        for metric in metrics
        if metric.qualified_id in wanted or metric.id in wanted
    ]


def clear_query_cache() -> None:
    """清空查询缓存（当前适配器无缓存，保留为兼容接口）。"""

    return None
