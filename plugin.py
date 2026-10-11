"""麦麦饭卡插件入口。

提供四块能力：
1. ``query_meal_card`` 工具：供 LLM 查询额度 / 用量；
2. ``/饭卡`` 指令：在指令来源处回复，可选文字 / LLM 转述 / 图片；
3. 定时播报与条件警报：由 :class:`~.scheduler.ScheduleLoop` 后台驱动；
4. 平台适配：具体查询由 ``platforms/`` 下的适配器实现（内置 MiniMax TokenPlan、DeepSeek 开放平台与「自定义平台」）。

配置采用「固定槽位 + 数量控制」结构（见 ``config.py``）：平台 / 播报 / 警报各自按数量生成
``platform1..N`` / ``broadcast1..N`` / ``alert1..N`` 槽位；改大数量后重载一次插件即生成新槽位，
改小数量并重载会清空超出的槽位（由 :meth:`MealCardPlugin.normalize_plugin_config` 收口）。
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

import logging
import re
import tomllib
from pathlib import Path

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import (
    ALERT_SLOTS,
    BROADCAST_SLOTS,
    PLATFORM_SLOTS,
    MealCardConfig,
)
from .delivery import send_content
from .llm_rewrite import LlmCallError, generate_rewrite
from .permissions import check_conversation_access, filter_requested_keys, resolve_allowed_keys
from .platforms import MetricValue
from .query_engine import available_metric_ids, describe_platforms, run_all
from .renderer import build_image
from .scheduler import ScheduleLoop
from .text_report import render_text_report

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
            ("access", "指令与权限", "shield", 6, ["command", "llm_tool_permission", "command_permission"]),
            ("advanced", "渲染与上限", "image", 7, ["render", "limits"]),
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

        return {
            "success": True,
            "content": render_text_report(results, self.config, describe_platforms(self.config)),
            "items": _describe_metrics(results),
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

    return any(str(slot.get(key) or "").strip() for key in ("key", "name", "api_key", "custom_base_url"))


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


def create_plugin() -> MealCardPlugin:
    """创建麦麦饭卡插件实例。"""

    return MealCardPlugin()
