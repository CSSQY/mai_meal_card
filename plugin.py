"""麦麦饭卡插件入口。

提供四块能力：
1. ``query_meal_card`` 工具：供 LLM 查询额度 / 用量；
2. ``/饭卡`` 指令：在指令来源处回复，可选文字 / LLM 转述 / 图片；
3. 定时播报与条件警报：由 :class:`~.scheduler.ScheduleLoop` 后台驱动；
4. 平台适配：具体查询由 ``platforms/`` 下的适配器实现（内置 MiniMax TokenPlan、DeepSeek 开放平台、火山方舟 Agent Plan 与「自定义平台」）。

配置采用「固定槽位 + 数量控制」结构（见 ``config.py``）：平台 / 播报 / 警报各自按数量生成
``platform1..N`` / ``broadcast1..N`` / ``alert1..N`` 槽位；改大数量后重载一次插件即生成新槽位，
改小数量并重载会清空超出的槽位（由 :meth:`MealCardPlugin.normalize_plugin_config` 收口）。
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

import logging
import re
import tomllib
from pathlib import Path

from maibot_sdk import API, Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import (
    ALERT_SLOTS,
    BROADCAST_SLOTS,
    PLATFORM_SLOTS,
    MealCardConfig,
)
from .delivery import send_content
from .history import (
    HistoryStore,
    build_curve,
    format_size,
    format_span,
    iter_recent_samples,
    resolve_metric_id,
    summarize_sample,
)
from .llm_rewrite import LlmCallError, generate_rewrite
from .permissions import check_conversation_access, filter_requested_keys, resolve_allowed_keys
from .platforms import MetricValue
from .platforms.base import KIND_COUNT, KIND_PERCENT
from .query_engine import available_metric_ids, describe_platforms, format_metric, run_all
from .renderer import build_image
from .scheduler import ScheduleLoop
from .text_report import render_text_report, strip_raw_metrics

logger = logging.getLogger(__name__)

# 宿主会把「引用/回复」的原文拼进消息文本（形如 ``[回复了某某的消息: 原消息内容]``），
# 位置可能在指令之前或之后。引用内容不是用户输入，必须剥离，否则会被当成指标参数。
_LEADING_QUOTE_RE = re.compile(r"^\s*\[回复[\s\S]*\]\s*(?=/饭卡)")

# 指令正则：允许前面带一个引用块（用户引用他人消息后发送 /饭卡 仍可用）。
_COMMAND_PATTERN = r"^(?:\[回复[\s\S]*\]\s*)?/饭卡(?:(?:\s+|\s*(?=\[回复))(?P<args>[\s\S]+))?$"

_USAGE_TEXT = (
    "用法：/饭卡 [指标] [全部|文字|图片|转述]\n"
    "  指标：逗号分隔的指标 id，例如 /饭卡 minimax.interval_remaining；留空使用「默认指标」配置\n"
    "  全部：查询全部指标\n"
    "  文字：本次用模板文本回复；图片：本次用图片回复；转述：本次用 LLM 风格化转述\n"
    "示例：/饭卡、/饭卡 全部、/饭卡 minimax.interval_remaining 文字"
)

_SEND_MODE_ALIASES = {
    "文字": "template",
    "文本": "template",
    "text": "template",
    "template": "template",
    "图片": "image",
    "image": "image",
    "转述": "llm",
    "llm": "llm",
}

_ALL_ALIASES = {"全部", "所有", "all"}

# WebUI「麦麦饭卡」页面用到的常量。
_HISTORY_FILE_NAME = "history.json"
"""历史记录文件名（位于宿主分配的插件数据目录内）。"""

_CHART_MAX_POINTS = 120
"""趋势曲线最多输出的点数。"""

_CHART_SLOT_LIMIT = 6
"""WebUI 页面里预置的曲线槽位数（与 webui.json 的节点数一一对应）；配置超出部分忽略。"""

_CHART_SLOT_WARNED = False
"""配置超出槽位数时是否已记过日志（避免每次刷新页面都刷屏）。"""

_HISTORY_TABLE_LIMIT = 50
"""历史记录表格展示的最近采样条数。"""

_KIND_FILTER_ALIASES = {
    "全部": "",
    "all": "",
    "百分比": "percent",
    "percent": "percent",
    "倒计时": "duration",
    "时长": "duration",
    "duration": "duration",
    "计数": "count",
    "count": "count",
    "文本": "text",
    "text": "text",
}

# 旧版配置里的分节名，迁移后需要从配置里移除。
_LEGACY_SECTIONS = ("platform", "minimax", "broadcast", "alerts")

# 旧版任务的「字段=值」条目允许的字段。
_LEGACY_BROADCAST_FIELDS = frozenset(
    {"name", "enabled", "times", "target", "keys", "mode", "condition", "content"}
)
_LEGACY_ALERT_FIELDS = frozenset(
    {"name", "enabled", "interval", "target", "keys", "condition", "mode", "content"}
)


class MealCardPlugin(MaiBotPlugin):
    """麦麦饭卡插件。"""

    config_model = MealCardConfig

    def __init__(self) -> None:
        """初始化插件实例与内部状态。"""

        super().__init__()
        self._scheduler = ScheduleLoop(self)
        self._history: Optional[HistoryStore] = None

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """插件加载：启动调度循环。"""

        self._scheduler.start()
        logger.info(
            "[meal_card] 插件已加载（平台槽位 %d / 播报槽位 %d / 警报槽位 %d），调度循环已启动",
            PLATFORM_SLOTS,
            BROADCAST_SLOTS,
            ALERT_SLOTS,
        )

    async def on_unload(self) -> None:
        """插件卸载：停止调度循环。"""

        await self._scheduler.stop()
        logger.info("[meal_card] 插件已卸载，调度循环已停止")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：唤醒调度循环并清空缓存。"""

        del config_data
        if scope != "self":
            return
        self._scheduler.notify_config_updated()
        logger.info("[meal_card] 插件配置已更新（version=%s），任务将按新配置重算", version)

    # ──── 配置归一化：数量收口 + 旧配置迁移 ────

    def normalize_plugin_config(self, config_data: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        """按数量收口槽位，并把旧版配置迁移到槽位结构。

        宿主在加载时会在 ``hasattr`` 检查后调用本方法，并用返回的 dict 覆盖配置；
        返回 ``(配置, 是否有变更)``。从磁盘读原始 ``config.toml`` 判定旧字段，避免依赖宿主
        版本预处理是否保留旧字段；不抛异常（最坏只是不迁移）。
        """

        data: Dict[str, Any] = dict(config_data or {})
        try:
            disk = _read_disk_config()
            changed = self._migrate_legacy(data, disk or {})
            if changed:
                logger.info("[meal_card] 检测到旧版配置，已迁移到槽位结构")
            if _enforce_counts(data):
                changed = True
            return data, changed
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响加载
            logger.warning("[meal_card] 配置归一化失败，按现有配置加载：%s", exc)
            return data, False

    def _migrate_legacy(self, data: Dict[str, Any], disk: Dict[str, Any]) -> bool:
        """把磁盘旧配置迁移到槽位结构，返回是否有改动。"""

        changed = False

        # A) [platform] + [minimax] → platform1
        legacy_platform = disk.get("platform")
        legacy_minimax = disk.get("minimax")
        if isinstance(legacy_platform, dict) or isinstance(legacy_minimax, dict):
            target = data.setdefault("platform1", {})
            if not isinstance(target, dict):
                target = {}
                data["platform1"] = target
            if not str(target.get("api_key") or "").strip():
                minimax = legacy_minimax if isinstance(legacy_minimax, dict) else {}
                for key in ("region", "api_key", "base_url"):
                    value = minimax.get(key)
                    if value not in (None, "") and target.get(key) != value:
                        target[key] = value
                        changed = True
                platform_section = legacy_platform if isinstance(legacy_platform, dict) else {}
                if str(platform_section.get("platform") or "") == "custom" and target.get("adapter") != "custom":
                    target["adapter"] = "custom"
                    changed = True

        # B) [broadcast].tasks（旧「字段=值」列表）→ broadcast1..N
        legacy_broadcast = disk.get("broadcast")
        if isinstance(legacy_broadcast, dict):
            changed = _migrate_legacy_tasks(
                data, legacy_broadcast.get("tasks"), "broadcast", BROADCAST_SLOTS, _LEGACY_BROADCAST_FIELDS
            ) or changed

        # C) [alerts].tasks → alert1..N
        legacy_alerts = disk.get("alerts")
        if isinstance(legacy_alerts, dict):
            changed = _migrate_legacy_tasks(
                data, legacy_alerts.get("tasks"), "alert", ALERT_SLOTS, _LEGACY_ALERT_FIELDS
            ) or changed

        # D) 移除旧分节
        for key in _LEGACY_SECTIONS:
            if data.pop(key, None) is not None:
                changed = True

        return changed

    # ──── WebUI 配置页：分标签页 ────

    def get_webui_config_schema(self, **kwargs: Any) -> Dict[str, Any]:
        """在默认 schema 基础上把分节组织成标签页，避免一屏糊在一起。"""

        try:
            schema = super().get_webui_config_schema(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 配置页绝不能 500
            logger.warning("[meal_card] 构建配置页 schema 失败，已回退空 schema：%s", exc)
            return {}
        if not (isinstance(schema, dict) and isinstance(schema.get("sections"), dict)):
            return schema

        sections = schema["sections"]
        active_platforms = _active_slot_count(sections, "platform", PLATFORM_SLOTS)
        active_broadcasts = _active_slot_count(sections, "broadcast", BROADCAST_SLOTS)
        active_alerts = _active_slot_count(sections, "alert", ALERT_SLOTS)

        groups: List[Tuple[str, str, str, int, List[str]]] = [
            ("basic", "插件", "settings", 1, ["plugin"]),
            ("platforms", "平台", "server", 2, [f"platform{i}" for i in range(1, active_platforms + 1)]),
            ("broadcasts", "播报", "clock", 3, [f"broadcast{i}" for i in range(1, active_broadcasts + 1)]),
            ("alerts", "警报", "bell", 4, [f"alert{i}" for i in range(1, active_alerts + 1)]),
            ("content", "展示与模板", "file-text", 5, ["display", "text_template", "llm_rewrite"]),
            ("record", "记录与统计", "chart", 6, ["history"]),
            ("access", "指令与权限", "shield", 7, ["command", "llm_tool_permission", "command_permission"]),
            ("advanced", "渲染与上限", "image", 8, ["render", "limits"]),
        ]

        tabs: List[Dict[str, Any]] = []
        for tab_id, title, icon, order, names in groups:
            present = [name for name in names if name in sections]
            if present:
                tabs.append({"id": tab_id, "title": title, "icon": icon, "order": order, "sections": present})
        if tabs:
            tabs.sort(key=lambda tab: tab["order"])
            schema["layout"] = {"type": "tabs", "tabs": tabs}
        return schema

    # ──── LLM 工具 ────

    @Tool(
        "query_meal_card",
        description=(
            "查询「饭卡」额度 / 用量（支持多个已配置的平台）。可指定指标 id"
            "（形如 minimax.interval_remaining；留空查询全部），也可用 metric 过滤指标类型："
            "percent=百分比、duration=倒计时、count=计数、all=全部。"
            "受插件配置的「LLM 工具查询权限」限制，只能查询该对话被放行的指标。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query_key",
                param_type=ToolParamType.STRING,
                description="指标 id（形如 minimax.interval_remaining），多个用逗号分隔；留空=全部",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="metric",
                param_type=ToolParamType.STRING,
                description="指标类型过滤：percent / duration / count / text / all（留空=全部）",
                required=False,
                default="",
            ),
        ],
    )
    async def handle_query_meal_card(
        self,
        query_key: str = "",
        metric: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """查询额度 / 用量并返回给 LLM。"""

        permission = self.config.llm_tool_permission
        group_id = str(kwargs.get("group_id") or "")
        user_id = str(kwargs.get("user_id") or "")

        if not check_conversation_access(permission, group_id=group_id, user_id=user_id):
            logger.info("[meal_card] 工具调用被对话流名单拦下：group=%s user=%s", group_id, user_id)
            return {"success": False, "content": "当前会话没有查询饭卡的权限（由配置「LLM 工具查询权限」决定）"}

        requested = _split_keys(query_key)
        if requested:
            allowed_keys, denied_keys = filter_requested_keys(
                permission, group_id=group_id, user_id=user_id, requested=requested
            )
            if denied_keys:
                return {
                    "success": False,
                    "content": f"不允许查询这些指标：{'、'.join(denied_keys)}（可查询范围由配置「LLM 工具查询权限」决定）",
                }
        else:
            allowed_keys = _allowed_key_list(permission, group_id=group_id, user_id=user_id)
            if allowed_keys is not None and not allowed_keys:
                return {
                    "success": False,
                    "content": "当前会话被配置为不允许查询任何指标（可查询范围由配置「LLM 工具查询权限」决定）",
                }

        kind_filter, kind_error = _normalize_kind_filter(metric)
        if kind_error:
            return {"success": False, "content": kind_error}

        results = await run_all(self.ctx, self.config, allowed_keys or (), kind_filter)
        if not results:
            return {"success": False, "content": _empty_result_hint(self.config)}

        visible = strip_raw_metrics(results, self.config)
        return {
            "success": True,
            "content": render_text_report(visible, self.config, describe_platforms(self.config)),
            "items": _describe_metrics(visible),
        }

    # ──── 指令 ────

    @Command(
        "meal_card",
        description="查询饭卡额度 / 用量（可指定指标与呈现方式）",
        pattern=_COMMAND_PATTERN,
    )
    async def handle_meal_card_command(
        self,
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """处理 ``/饭卡`` 指令。"""

        if not self.config.command.enabled:
            return False, "饭卡指令已禁用", True

        permission = self.config.command_permission
        if not check_conversation_access(
            permission,
            group_id=group_id,
            user_id=user_id,
            is_local_operator=bool(is_local_operator),
        ):
            await self._notify_no_permission(stream_id)
            return True, "无权限", True

        requested_keys, send_mode_override = self._parse_command_args(kwargs)

        if requested_keys:
            allowed_keys, denied_keys = filter_requested_keys(
                permission, group_id=group_id, user_id=user_id, requested=requested_keys
            )
            if denied_keys:
                await self.ctx.send.text(f"不允许查询这些指标：{'、'.join(denied_keys)}", stream_id)
                return False, "范围超出权限", True
        else:
            allowed_keys = _allowed_key_list(permission, group_id=group_id, user_id=user_id)
            if allowed_keys is not None and not allowed_keys:
                await self.ctx.send.text("当前会话被配置为不允许查询任何指标", stream_id)
                return False, "范围超出权限", True
            if not allowed_keys:
                default_keys = _split_keys(self.config.command.default_query_keys)
                if default_keys:
                    allowed_keys = default_keys

        results = await run_all(self.ctx, self.config, allowed_keys or ())
        if not results:
            await self.ctx.send.text(f"{_empty_result_hint(self.config)}\n\n{_USAGE_TEXT}", stream_id)
            return False, "无可用指标", True

        send_mode = send_mode_override or self.config.command.send_mode
        text, image_base64 = await self._build_content(results, send_mode, None)
        if not text and not image_base64:
            await self.ctx.send.text("未能生成任何内容", stream_id)
            return False, "生成内容失败", True

        sent = await send_content(self.ctx, stream_id, text, image_base64)
        if not sent:
            return False, "饭卡结果发送失败", True
        return True, "已发送饭卡结果", True

    async def _notify_no_permission(self, stream_id: str) -> None:
        """按配置决定是否回复「无权限」提示。"""

        if not self.config.command.notify_no_permission:
            return
        deny_message = str(self.config.command.deny_message or "").strip()
        if not deny_message:
            return
        await self.ctx.send.text(deny_message, stream_id)

    # ──── WebUI 页面（「麦麦饭卡」）的查询与操作 ────

    def _get_history(self) -> Optional[HistoryStore]:
        """懒加载历史存储（位于宿主分配的插件持久化数据目录）。"""

        if self._history is not None:
            return self._history
        paths = getattr(self.ctx, "paths", None)
        data_dir = getattr(paths, "data_dir", None) if paths is not None else None
        if data_dir is None:
            logger.warning("[meal_card] 宿主未提供插件数据目录，WebUI 历史记录不可用")
            return None
        self._history = HistoryStore(Path(data_dir) / _HISTORY_FILE_NAME)
        logger.info("[meal_card] 历史记录已挂载：%s", data_dir)
        return self._history

    @API("webui_summary", description="读取饭卡历史记录的概览统计（WebUI 页面用）", version="1")
    async def webui_summary(self) -> Dict[str, Any]:
        """返回页面顶部概览卡片所需的统计值。"""

        section = self.config.history
        interval = _coerce_interval(getattr(section, "interval_seconds", 1800))
        store = self._get_history()
        stats = store.stats() if store is not None else {"count": 0, "first_ts": 0, "last_ts": 0, "file_size": 0, "metric_count": 0}
        meta = store.meta if store is not None else {}
        auto_metric = resolve_metric_id(meta, "")
        auto_label = f"{meta[auto_metric].get('name') or auto_metric}（{auto_metric}）" if auto_metric else "暂无可用指标"
        return {
            "status": "已启用" if getattr(section, "enabled", False) else "已停用",
            "interval": _format_seconds(interval),
            "sample_count": str(stats["count"]),
            "span": format_span(stats["first_ts"], stats["last_ts"]) or "暂无记录",
            "file_size": format_size(stats["file_size"]),
            "metric_total": str(stats["metric_count"]),
            "auto_metric": auto_label,
            "retention": _describe_retention(section),
            "storage_ready": "可用" if store is not None else "不可用（宿主未提供数据目录）",
        }

    @API("webui_current", description="读取最近一次采样得到的各平台额度与用量", version="1")
    async def webui_current(self) -> Dict[str, Any]:
        """返回「当前额度与用量」表格数据（取最近一条采样，避免实时请求超时）。"""

        store = self._get_history()
        rows: List[Dict[str, Any]] = []
        updated = "暂无记录"
        if store is not None:
            latest = store.latest()
            if latest is not None:
                updated = str(latest.get("time") or "")
                meta = store.meta
                for metric_id, value in (latest.get("values") or {}).items():
                    if isinstance(value, (int, float)):
                        rows.append(self._current_row(str(metric_id), float(value), meta.get(str(metric_id)) or {}))
                rows.sort(key=lambda item: (str(item.get("platform") or ""), str(item.get("metric") or "")))
        return {"rows": rows, "updated": updated}

    @API("webui_chart", description="读取指定指标的平滑趋势曲线数据", version="1")
    async def webui_chart(self, metric: str = "") -> Dict[str, Any]:
        """把历史序列平滑重采样为折线图数据（宿主折线为直线段，靠加密点呈现平滑）。"""

        store = self._get_history()
        if store is None:
            return {"points": [], "metric": "", "metric_name": "", "note": "宿主未提供插件数据目录，无法读取历史记录"}
        meta = store.meta
        if not meta:
            return {"points": [], "metric": "", "metric_name": "", "note": "暂无历史数据：启用「记录与统计」后会按检查周期自动采样"}
        resolved = resolve_metric_id(meta, metric)
        if not resolved:
            return {
                "points": [],
                "metric": "",
                "metric_name": "",
                "note": f"找不到指标「{metric}」；可在上方「当前额度与用量」表的「指标 id」列查看可用 id",
            }
        points = build_curve(store.series(resolved), max_points=_CHART_MAX_POINTS)
        name = str((meta.get(resolved) or {}).get("name") or resolved)
        return {
            "points": points,
            "metric": resolved,
            "metric_name": name,
            "note": "" if points else "该指标暂无可用数值",
        }

    @API("webui_curves", description="读取配置的多指标趋势曲线（一次返回多张平滑曲线）", version="1")
    async def webui_curves(self) -> Dict[str, Any]:
        """按配置的「趋势曲线指标」一次返回多组曲线数据。

        页面为每条指标预置一个图表槽位（最多 :data:`_CHART_SLOT_LIMIT` 个）：本方法固定返回
        同样长度的数组，未配置或取不到数据的槽位用空数组 + ``flags[i] = False`` 占位，
        页面据此把该槽位隐藏起来。
        """

        global _CHART_SLOT_WARNED

        configured = [
            str(item).strip()
            for item in (getattr(self.config.history, "chart_metrics", []) or [])
            if str(item or "").strip()
        ]
        if len(configured) > _CHART_SLOT_LIMIT and not _CHART_SLOT_WARNED:
            _CHART_SLOT_WARNED = True
            logger.warning(
                "[meal_card] 趋势曲线指标的配置条数（%d）超过上限 %d，仅显示前 %d 条",
                len(configured),
                _CHART_SLOT_LIMIT,
                _CHART_SLOT_LIMIT,
            )

        store = self._get_history()
        meta = store.meta if store is not None else {}
        series: List[List[Dict[str, Any]]] = []
        titles: List[str] = []
        flags: List[bool] = []
        problems: List[str] = []

        for index in range(_CHART_SLOT_LIMIT):
            wanted = configured[index] if index < len(configured) else ""
            if not wanted:
                series.append([])
                titles.append("")
                flags.append(False)
                continue
            if store is None:
                series.append([])
                titles.append(wanted)
                flags.append(False)
                problems.append("宿主未提供插件数据目录，无法读取历史记录")
                continue
            resolved = resolve_metric_id(meta, wanted)
            if not resolved:
                series.append([])
                titles.append(wanted)
                flags.append(False)
                problems.append(f"找不到指标「{wanted}」")
                continue
            points = build_curve(store.series(resolved), max_points=_CHART_MAX_POINTS)
            name = str((meta.get(resolved) or {}).get("name") or resolved)
            series.append(points)
            titles.append(f"{name}（{resolved}）")
            flags.append(bool(points))
            if not points:
                problems.append(f"{resolved} 暂无可用数值")

        if not configured:
            note = (
                f"未配置趋势指标：可在插件配置的「记录与统计 → 趋势曲线指标」中添加"
                f"（最多 {_CHART_SLOT_LIMIT} 条），每条会渲染成一张独立的平滑曲线图"
            )
        elif len(configured) > _CHART_SLOT_LIMIT:
            note = f"共配置 {len(configured)} 条，仅显示前 {_CHART_SLOT_LIMIT} 条；" + "；".join(problems)
        else:
            note = "；".join(problems)

        return {
            "series": series,
            "titles": titles,
            "flags": flags,
            "note": note,
            "count": sum(1 for flag in flags if flag),
        }

    @API("webui_history", description="读取最近的采样记录列表", version="1")
    async def webui_history(self) -> Dict[str, Any]:
        """返回最近若干条采样记录的表格数据。"""

        store = self._get_history()
        rows: List[Dict[str, Any]] = []
        if store is not None:
            meta = store.meta
            for sample in iter_recent_samples(store.samples, _HISTORY_TABLE_LIMIT):
                values = sample.get("values") or {}
                rows.append(
                    {
                        "time": str(sample.get("time") or ""),
                        "count": len(values),
                        "summary": summarize_sample(sample, meta),
                    }
                )
        return {"rows": rows}

    @API("webui_sample", description="立即采样一次并写入历史记录", version="1")
    async def webui_sample(self) -> Dict[str, Any]:
        """手动触发一次采样（受检查周期之外的即时补充）。"""

        if not getattr(self.config.history, "enabled", False):
            return {"success": False, "message": "历史记录已停用，请先在插件配置的「记录与统计」中开启"}
        written = await self._scheduler.record_now()
        return {
            "success": written,
            "message": "已记录一条采样" if written else "本次采样没有可用数值（请检查平台配置与插件日志）",
        }

    @API("webui_clear", description="清空饭卡历史记录", version="1")
    async def webui_clear(self) -> Dict[str, Any]:
        """清空历史样本（指标名称等元信息保留）。"""

        store = self._get_history()
        if store is None:
            return {"success": False, "message": "宿主未提供插件数据目录，无法清空"}
        store.clear()
        return {"success": True, "message": "历史记录已清空"}

    @API("webui_apply", description="应用曲线指标选择（触发页面数据刷新）", version="1")
    async def webui_apply(self, metric: str = "") -> Dict[str, Any]:
        """按钮用的空操作：真正的刷新由宿主前端在操作完成后重新拉取查询数据。"""

        del metric
        return {"success": True}

    def _current_row(self, metric_id: str, value: float, info: Dict[str, Any]) -> Dict[str, Any]:
        """把一条采样值渲染为表格行（复用与报告一致的展示格式化）。"""

        kind = str(info.get("kind") or KIND_COUNT)
        prefix, _, suffix = metric_id.partition(".")
        metric = MetricValue(
            id=suffix or metric_id,
            name=str(info.get("name") or metric_id),
            kind=kind,
            value=value,
            unit=str(info.get("unit") or ""),
            platform_key=prefix,
            platform_name=str(info.get("platform") or ""),
            progress=(value / 100.0) if kind == KIND_PERCENT else None,
        )
        try:
            display = format_metric(metric, self.config).display
        except Exception as exc:  # noqa: BLE001 - 展示失败不能影响页面
            logger.warning("[meal_card] 格式化历史指标失败：%s", exc)
            display = f"{value:g}"
        return {
            "platform": metric.platform_name or "—",
            "name": metric.name,
            "metric": metric_id,
            "value": display or f"{value:g}",
            "status": "正常",
        }

    @staticmethod
    def _parse_command_args(kwargs: Dict[str, Any]) -> Tuple[List[str], str]:
        """解析指令参数，返回 ``(指标 id 列表, 发送方式覆盖)``。"""

        raw_args = _extract_args(kwargs)
        if not raw_args:
            return [], ""

        keys: List[str] = []
        send_mode = ""
        for token in re.split(r"[\s,，]+", raw_args.strip()):
            if not token:
                continue
            if token in _ALL_ALIASES:
                keys = []
                continue
            if token in _SEND_MODE_ALIASES:
                send_mode = _SEND_MODE_ALIASES[token]
                continue
            keys.append(token)
        return keys, send_mode

    # ──── 内容生成 ────

    async def _build_content(
        self,
        results: Sequence[MetricValue],
        send_mode: str,
        content_override: Optional[str],
    ) -> Tuple[str, Optional[str]]:
        """按发送方式生成内容，返回 ``(文本, 图片 base64)``。"""

        mode = str(send_mode or "template").strip().lower()
        platform_name = describe_platforms(self.config)
        if mode == "image":
            image_base64, render_error = await build_image(self.ctx, results, self.config, platform_name)
            if image_base64 is not None:
                return "", image_base64
            logger.error("[meal_card] 图片渲染失败，已回退为文本：%s", render_error)
            return render_text_report(results, self.config, platform_name, content_override), None
        if mode == "llm":
            try:
                return await generate_rewrite(self.ctx, self.config, results, platform_name), None
            except LlmCallError as exc:
                logger.error("[meal_card] LLM 转述失败，已回退为文本：%s", exc)
                return render_text_report(results, self.config, platform_name, content_override), None
        return render_text_report(results, self.config, platform_name, content_override), None


# ────────────────────────── 配置归一化辅助 ──────────────────────────


def _read_disk_config() -> Optional[Dict[str, Any]]:
    """读磁盘 ``config.toml`` 原始内容；不存在或解析异常时返回 ``None``。"""

    path = Path(__file__).resolve().parent / "config.toml"
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[meal_card] 读取磁盘配置失败：%s", exc)
        return None
    return data if isinstance(data, dict) else None


def _active_slot_count(sections: Dict[str, Any], prefix: str, generated: int) -> int:
    """返回配置页里应当展示的槽位数量（按实际存在的分节数）。"""

    return sum(1 for i in range(1, generated + 1) if f"{prefix}{i}" in sections)


def _enforce_counts(data: Dict[str, Any]) -> bool:
    """把超出「数量控制」的槽位清空为默认值，返回是否有改动。

    只清空「确实配置过」的槽位（避免每次加载都判定为有改动而反复改写磁盘）。
    """

    plugin_section = data.get("plugin")
    if not isinstance(plugin_section, dict):
        return False

    changed = False
    plans = (
        ("platform_count", "platform", PLATFORM_SLOTS, _platform_slot_used),
        ("broadcast_count", "broadcast", BROADCAST_SLOTS, _broadcast_slot_used),
        ("alert_count", "alert", ALERT_SLOTS, _alert_slot_used),
    )
    for count_key, prefix, generated, is_used in plans:
        try:
            count = max(1, min(generated, int(plugin_section.get(count_key, 1))))
        except (TypeError, ValueError):
            logger.warning("[meal_card] %s 非整数，已按 1 处理且不清空任何槽位", count_key)
            count = 1
        for index in range(count + 1, generated + 1):
            slot = data.get(f"{prefix}{index}")
            if isinstance(slot, dict) and is_used(slot):
                data[f"{prefix}{index}"] = {}
                changed = True
    return changed


def _platform_slot_used(slot: Dict[str, Any]) -> bool:
    """判断平台槽位是否已填写过有意义的内容。"""

    return any(
        str(slot.get(key) or "").strip()
        for key in ("key", "name", "api_key", "access_key", "secret_key", "custom_base_url")
    )


def _broadcast_slot_used(slot: Dict[str, Any]) -> bool:
    """判断播报槽位是否已填写过有意义的内容。"""

    return bool(slot.get("target_groups") or slot.get("target_users") or str(slot.get("name") or "").strip())


def _alert_slot_used(slot: Dict[str, Any]) -> bool:
    """判断警报槽位是否已填写过有意义的内容。"""

    return bool(slot.get("target_groups") or slot.get("target_users") or str(slot.get("condition") or "").strip())


def _migrate_legacy_tasks(
    data: Dict[str, Any],
    raw_tasks: Any,
    prefix: str,
    generated: int,
    allowed: frozenset,
) -> bool:
    """把旧版「字段=值」任务列表迁移到 ``<prefix>1..N`` 槽位，返回是否有改动。"""

    if not isinstance(raw_tasks, list):
        return False

    changed = False
    index = 0
    for raw in raw_tasks:
        fields = _parse_legacy_entry(raw, allowed)
        if fields is None:
            continue
        index += 1
        if index > generated:
            logger.warning("[meal_card] 旧版 %s 任务数量超过当前槽位数（%d），超出部分未迁移", prefix, generated)
            break
        target = data.setdefault(f"{prefix}{index}", {})
        if not isinstance(target, dict):
            target = {}
            data[f"{prefix}{index}"] = target

        targets = fields.get("target", [])
        groups = [item.split(":", 1)[1].strip() for item in targets if item.split(":", 1)[0].strip() in {"群", "群聊", "group"}]
        users = [item.split(":", 1)[1].strip() for item in targets if item.split(":", 1)[0].strip() in {"用户", "个人", "私聊", "user"}]
        if groups and target.get("target_groups") != groups:
            target["target_groups"] = groups
            changed = True
        if users and target.get("target_users") != users:
            target["target_users"] = users
            changed = True

        for key in ("name", "keys", "mode", "condition", "content", "times", "interval"):
            value = _legacy_first(fields, key)
            if value and target.get(key) != value:
                target[key] = value
                changed = True
        if _legacy_first(fields, "enabled") in {"1", "true", "yes", "on", "是", "启用"}:
            if target.get("enabled") is not True:
                target["enabled"] = True
                changed = True
    return changed


def _parse_legacy_entry(raw: Any, allowed: frozenset) -> Optional[Dict[str, List[str]]]:
    """解析旧版「字段=值」条目（迁移专用）。"""

    text = str(raw or "").strip()
    if not text:
        return None
    fields: Dict[str, List[str]] = {}
    for chunk in text.split(";"):
        item = chunk.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip().lower()
        if name and name in allowed:
            fields.setdefault(name, []).append(value.strip())
    return fields or None


def _legacy_first(fields: Dict[str, List[str]], name: str) -> str:
    """取旧版字段的第一个值。"""

    values = fields.get(name)
    return values[0] if values else ""


# ────────────────────────── 通用辅助 ──────────────────────────


def _strip_quote_blocks(text: str) -> str:
    """去掉宿主拼进消息文本里的「引用/回复」块。

    宿主会把被引用消息的原文并入 ``processed_plain_text``（形如
    ``[回复了某某的消息: 原消息内容]`` / ``[回复了一条消息，但原消息已无法访问]``），
    位置可能在指令之前或之后。引用内容不是用户输入，若被当作指标参数会导致
    「没有可查询的指标」误报，因此统一剥离。
    """

    cleaned = str(text or "")
    cleaned = _LEADING_QUOTE_RE.sub("", cleaned, count=1)
    start = cleaned.find("[回复")
    if start >= 0:
        tail = cleaned[start:]
        end = tail.rfind("]")
        cleaned = cleaned[:start] + (tail[end + 1 :] if end >= 0 else "")
    return cleaned.strip()


def _extract_args(kwargs: Dict[str, Any]) -> str:
    """从指令回调参数里取出参数文本（已剔除引用内容）。"""

    matched_groups = kwargs.get("matched_groups")
    if isinstance(matched_groups, dict):
        args = _strip_quote_blocks(str(matched_groups.get("args") or ""))
        if args:
            return args
    raw_text = _strip_quote_blocks(str(kwargs.get("text") or ""))
    if raw_text.startswith("/饭卡"):
        return _strip_quote_blocks(raw_text[len("/饭卡") :]).strip()
    return raw_text


def _split_keys(raw: str) -> List[str]:
    """把指标 id 字符串拆成列表。"""

    text = str(raw or "").strip()
    if not text or text == "*":
        return []
    return [key.strip() for key in re.split(r"[,，]", text) if key.strip()]


def _allowed_key_list(section, *, group_id: str = "", user_id: str = "") -> Optional[List[str]]:
    """返回放行指标 id 列表。

    Returns:
        Optional[List[str]]: ``None`` 表示全部放行（不限）；``[]`` 表示全部禁止；
        否则为放行指标列表。
    """

    allowed = resolve_allowed_keys(section, group_id=group_id, user_id=user_id)
    if allowed is None:
        return None
    return sorted(allowed)


def _normalize_kind_filter(raw: str) -> Tuple[str, str]:
    """把工具参数 ``metric`` 规范化为指标类型过滤值。

    Returns:
        Tuple[str, str]: ``(类型过滤值, 错误提示)``；错误提示非空表示参数不合法。
    """

    text = str(raw or "").strip().lower()
    if not text:
        return "", ""
    if text not in _KIND_FILTER_ALIASES:
        return "", f"不支持的 metric：{raw}（可填 percent / duration / count / text / all）"
    return _KIND_FILTER_ALIASES[text], ""


def _describe_metrics(results: Sequence[MetricValue]) -> List[Dict[str, Any]]:
    """把指标转换为可序列化结构，供 LLM 使用。"""

    return [
        {
            "id": metric.qualified_id,
            "name": metric.name,
            "platform": metric.platform_name,
            "kind": metric.kind,
            "value": metric.value,
            "display": metric.display,
            "progress": metric.progress,
            "ok": metric.ok,
            "error": metric.error,
        }
        for metric in results
    ]


def _empty_result_hint(config: MealCardConfig) -> str:
    """构造「没有可用指标」的中文提示（含当前可用指标清单）。"""

    catalog = "、".join(available_metric_ids(config))
    base = "没有可查询的指标"
    if catalog:
        return f"{base}（当前可用指标：{catalog}；请检查平台配置与查询权限）"
    return f"{base}（请先在「平台」页填写平台与密钥）"


def _coerce_interval(raw: Any) -> int:
    """把配置里的检查周期规范化为秒数（非法值回退 1800）。"""

    try:
        return max(int(raw), 30)
    except (TypeError, ValueError):
        return 1800


def _format_seconds(seconds: int) -> str:
    """把秒数格式化为中文时长（如「30 分钟」「1 小时 30 分钟」）。"""

    total = max(int(seconds), 0)
    if total % 86400 == 0 and total >= 86400:
        return f"{total // 86400} 天"
    if total % 3600 == 0 and total >= 3600:
        return f"{total // 3600} 小时"
    if total % 60 == 0 and total >= 60:
        return f"{total // 60} 分钟"
    return f"{total} 秒"


def _describe_retention(section: Any) -> str:
    """把保留策略描述成一句中文（用于概览卡片）。"""

    mode = str(getattr(section, "retention_mode", "count") or "count").strip().lower()
    if mode == "days":
        return f"按天数保留最近 {int(getattr(section, 'retention_days', 30))} 天"
    if mode == "size":
        return f"按文件大小保留，上限 {float(getattr(section, 'retention_size_mb', 5.0)):g} MB"
    return f"按检查次数保留最近 {int(getattr(section, 'retention_count', 2880))} 条"


def create_plugin() -> MealCardPlugin:
    """创建麦麦饭卡插件实例。"""

    return MealCardPlugin()
