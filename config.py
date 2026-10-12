"""麦麦饭卡插件配置模型。

采用「固定槽位 + 数量控制」结构：

- ``[plugin]`` 里的 ``platform_count`` / ``broadcast_count`` / ``alert_count`` 决定生成并显示几个槽位；
- 槽位在**模块导入时**按磁盘 ``config.toml`` 已保存的数量生成（``platform1..platformN`` 等），
  每个槽位都是独立的表单分节（各自字段、各自开关），避免手写「分号分隔字符串」；
- 把数量**调大**后，到插件管理处「禁用再启用」（或重启麦麦）触发重新导入即可生成新槽位；
- 把数量**调小**并重载，超出部分的槽位配置会被清空（见 ``plugin.normalize_plugin_config``）。

顶层 ``MealCardConfig`` 用 ``create_model`` 动态绑定这些槽位字段。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from datetime import time as datetime_time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from maibot_sdk import PluginConfigBase
from pydantic import Field, create_model

logger = logging.getLogger(__name__)

# 插件 / 配置版本号（plugin.py、_manifest.json 与本文件保持一致）。
PLUGIN_VERSION = "1.1.2"

DEFAULT_TEXT_TEMPLATE = "🍚 饭卡状态（{date}）\n{items}\n{unavailable}"

DEFAULT_LLM_PROMPT = (
    "请用自然、口语化的中文，把下面这份额度/用量数据转述给群友或用户。要求：保留全部数字，"
    "不要编造、不要省略任何一项，不要输出表格或代码块，控制在 8 行以内。"
)


def _list_field(placeholder: str, hint: str) -> Dict[str, object]:
    """构造字符串列表字段的 UI 元数据。

    宿主会把 ``x-widget`` 规范化为 ``ui_type``，插件配置页按 ``ui_type`` 选择控件：
    只有 ``list`` 会渲染成「输入框 + 添加按钮 + 已添加项」的编辑器。
    """

    return {"label": "", "hint": hint, "placeholder": placeholder, "x-widget": "list"}


def _textarea_field(label: str, hint: str, rows: int = 6, placeholder: str = "") -> Dict[str, object]:
    """构造多行文本字段的 UI 元数据。"""

    return {
        "label": label,
        "hint": hint,
        "x-widget": "textarea",
        "rows": rows,
        "placeholder": placeholder,
    }


# ────────────────────────── 槽位数量（导入期读磁盘） ──────────────────────────

# 槽位数量 = 磁盘 config.toml 已保存的计数（首次安装/读不到时取默认值）。固定槽位天生要在
# 配置模型里预先生成 N 个字段，故 N 只能在导入时确定；改大后重载一次即按新数量生成。


def _saved_counts() -> Tuple[int, int, int]:
    """读磁盘 ``config.toml`` 已保存的槽位数量，返回 ``(平台数, 播报数, 警报数)``。

    读不到或异常时回退到默认值（平台 1、播报 1、警报 1），绝不让导入失败。
    """

    defaults = (1, 1, 1)
    try:
        path = Path(__file__).resolve().parent / "config.toml"
        if not path.exists():
            return defaults
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        plugin_section = data.get("plugin") if isinstance(data, dict) else None
        if not isinstance(plugin_section, dict):
            return defaults
        return (
            max(1, int(plugin_section.get("platform_count", 1))),
            max(1, int(plugin_section.get("broadcast_count", 1))),
            max(1, int(plugin_section.get("alert_count", 1))),
        )
    except Exception as exc:  # noqa: BLE001 - 读不到就按默认值，绝不让导入失败
        logger.debug("[meal_card] 读取已保存槽位数量失败、按默认值处理：%s", exc)
        return defaults


PLATFORM_SLOTS, BROADCAST_SLOTS, ALERT_SLOTS = _saved_counts()


# ────────────────────────── 插件基础 ──────────────────────────


class GeneralSectionConfig(PluginConfigBase):
    """通用设置。"""

    __ui_label__ = "通用设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="总开关，关闭后插件不再响应指令与工具，定时任务也停止",
        json_schema_extra={"label": "启用插件", "hint": "关闭后 /饭卡、LLM 工具与定时任务全部停止"},
    )
    platform_count: int = Field(
        default=1,
        ge=1,
        description="生成并显示几个平台槽位（无硬上限；改大后需重载一次插件才生成新槽位）",
        json_schema_extra={
            "label": "平台数量控制",
            "hint": "平台槽位个数，无上限。改大后需保存并「禁用再启用」插件（或重启麦麦）方生成新槽位；"
            "调小并重载会清空超出部分的平台配置。",
            "step": 1,
        },
    )
    broadcast_count: int = Field(
        default=1,
        ge=1,
        description="生成并显示几个定时播报槽位（无硬上限；改大后需重载一次插件才生成新槽位）",
        json_schema_extra={
            "label": "播报数量控制",
            "hint": "播报任务槽位个数，无上限。改大后需保存并「禁用再启用」插件（或重启麦麦）方生成新槽位。",
            "step": 1,
        },
    )
    alert_count: int = Field(
        default=1,
        ge=1,
        description="生成并显示几个警报槽位（无硬上限；改大后需重载一次插件才生成新槽位）",
        json_schema_extra={
            "label": "警报数量控制",
            "hint": "警报任务槽位个数，无上限。改大后需保存并「禁用再启用」插件（或重启麦麦）方生成新槽位。",
            "step": 1,
        },
    )
    config_version: str = Field(
        default=PLUGIN_VERSION,
        description="配置版本号（请勿手动修改）",
        json_schema_extra={"label": "配置版本", "hint": "插件用它判断是否升级配置结构，请勿手动修改"},
    )


# ────────────────────────── 平台槽位 ──────────────────────────


class PlatformSlotConfig(PluginConfigBase):
    """单个平台槽位：选择平台类型并填写该平台所需参数。"""

    enabled: bool = Field(
        default=True,
        description="是否启用此平台（关闭只是临时停用、不清空配置）",
        json_schema_extra={"label": "启用此平台", "hint": "关闭后此平台不参与查询"},
    )
    adapter: Literal["minimax", "deepseek", "custom"] = Field(
        default="minimax",
        description="平台类型：minimax=内置 MiniMax TokenPlan 适配；deepseek=内置 DeepSeek 开放平台适配（余额查询）；custom=自定义平台（自行填写地址与取值规则）",
        json_schema_extra={
            "label": "平台类型",
            "hint": "minimax=内置适配（选区域 + 填 TokenPlan 订阅密钥）；deepseek=内置适配（填 API Key 即可查余额）；"
            "custom=自定义平台（自行填写接口地址、鉴权与取值表达式）",
        },
    )
    key: str = Field(
        default="",
        description="平台标识：作为指标 id 的前缀（如 minimax.interval_remaining）；留空则用平台槽位编号（platform1、platform2…）",
        json_schema_extra={
            "label": "平台标识",
            "hint": "用于指标 id 前缀，建议用字母/数字/下划线（如 minimax、mm-cn）；留空则自动用 platform1 这类编号",
            "placeholder": "minimax",
        },
    )
    name: str = Field(
        default="",
        description="平台显示名称，出现在报告与日志中；留空则用平台类型名称",
        json_schema_extra={"label": "显示名称", "hint": "留空则用平台类型名称（如 MiniMax TokenPlan）", "placeholder": "MiniMax 国内"},
    )

    # ── 平台类型 = minimax 时生效 ──
    region: Literal["cn", "global"] = Field(
        default="cn",
        description="MiniMax 套餐购买地区：cn=国内版（platform.minimaxi.com）；global=国际版（platform.minimax.io）",
        json_schema_extra={
            "label": "区域（MiniMax）",
            "hint": "仅「平台类型=minimax」时生效：选择套餐购买的地区，选错会返回 401",
        },
    )
    api_key: str = Field(
        default="",
        description="平台密钥：minimax 填 TokenPlan 订阅密钥（sk-cp 开头）；deepseek 填开放平台 API Key（sk- 开头）",
        json_schema_extra={
            "label": "API 密钥（minimax / deepseek）",
            "hint": "「平台类型=minimax」填 TokenPlan 订阅密钥（sk-cp 开头，与按量付费 API Key 不通用）；"
            "「平台类型=deepseek」填开放平台 API Key（sk- 开头）",
            "x-widget": "password",
            "placeholder": "sk-cp-… 或 sk-…",
        },
    )
    base_url: str = Field(
        default="",
        description="接口基地址覆盖（minimax / deepseek 通用）；留空则用官方地址",
        json_schema_extra={
            "label": "自定义基地址（minimax / deepseek）",
            "hint": "一般留空：minimax 按「区域」自动选择官方地址、deepseek 默认 https://api.deepseek.com；"
            "仅在官方域名变更或走自建代理时才需要填",
            "placeholder": "https://api.deepseek.com",
        },
    )

    # ── 平台类型 = custom 时生效 ──
    custom_base_url: str = Field(
        default="",
        description="自定义平台的接口基地址（含协议与域名，可带路径前缀）",
        json_schema_extra={
            "label": "接口基地址（自定义）",
            "hint": "仅「平台类型=custom」时生效：如 https://api.example.com",
            "placeholder": "https://api.example.com",
        },
    )
    custom_path: str = Field(
        default="",
        description="接口路径（相对基地址），或完整 URL",
        json_schema_extra={
            "label": "接口路径（自定义）",
            "hint": "仅「平台类型=custom」时生效：如 /api/user/self",
            "placeholder": "/api/user/self",
        },
    )
    custom_method: Literal["GET", "POST"] = Field(
        default="GET",
        description="请求方法",
        json_schema_extra={"label": "请求方法（自定义）", "hint": "仅「平台类型=custom」时生效"},
    )
    custom_auth_mode: Literal["none", "bearer", "header", "basic"] = Field(
        default="bearer",
        description="鉴权方式：none=不带鉴权；bearer=Authorization: Bearer <值>；header=把「名=值」作为请求头；basic=Basic 用户名:密码",
        json_schema_extra={
            "label": "鉴权方式（自定义）",
            "hint": "仅「平台类型=custom」时生效：bearer 的鉴权值填密钥本身；header 填「名=值」；basic 填「用户名:密码」",
        },
    )
    custom_auth_value: str = Field(
        default="",
        description="鉴权值：bearer 填密钥；header 填「名=值」；basic 填「用户名:密码」",
        json_schema_extra={
            "label": "鉴权值（自定义）",
            "hint": "仅「平台类型=custom」时生效；none 时留空",
            "x-widget": "password",
        },
    )
    custom_headers: List[str] = Field(
        default_factory=list,
        description="附加请求头，每条「名=值」",
        json_schema_extra={
            **_list_field("X-Custom=1", "仅「平台类型=custom」时生效：填「名=值」后点 + 添加，可加多条"),
            "label": "附加请求头（自定义）",
        },
    )
    custom_value_expr: str = Field(
        default="",
        description="取值表达式：返回 JSON 时填点号路径（如 data.quota）；返回纯文本时留空",
        json_schema_extra={
            "label": "取值表达式（自定义）",
            "hint": "仅「平台类型=custom」时生效：JSON 填点号路径（如 data.quota）；纯文本数字留空",
            "placeholder": "data.quota",
        },
    )
    custom_value_type: Literal["quota", "usage", "other"] = Field(
        default="quota",
        description="取值含义：quota=额度（按小数位显示）；usage=用量（进度条）；other=其他",
        json_schema_extra={
            "label": "取值含义（自定义）",
            "hint": "仅「平台类型=custom」时生效：决定展示方式（额度/用量/其他）",
        },
    )
    custom_unit: str = Field(
        default="",
        description="展示单位（如 美元 / 积分 / 鸡蛋）",
        json_schema_extra={"label": "单位（自定义）", "hint": "仅「平台类型=custom」时生效", "placeholder": "美元"},
    )
    custom_ratio: str = Field(
        default="",
        description="换算比例 a:b（a 原始单位 = b 显示单位）；留空按 1:1",
        json_schema_extra={
            "label": "换算比例（自定义）",
            "hint": "仅「平台类型=custom」时生效：如 500000:1 表示 500000 原始单位 = 1 显示单位",
            "placeholder": "500000:1",
        },
    )
    custom_limit_expr: str = Field(
        default="",
        description="上限表达式（取值方式同「取值表达式」），用于计算用量进度条；留空则不显示进度条",
        json_schema_extra={
            "label": "上限表达式（自定义）",
            "hint": "仅「平台类型=custom」时生效：可选，用于用量进度条",
            "placeholder": "data.total",
        },
    )


# ────────────────────────── 播报 / 警报槽位 ──────────────────────────


class BroadcastSlotConfig(PluginConfigBase):
    """单个定时播报任务槽位。"""

    enabled: bool = Field(
        default=False,
        description="是否启用此播报任务",
        json_schema_extra={"label": "启用此播报", "hint": "默认关闭；开启前请先填好时刻与目标"},
    )
    name: str = Field(
        default="",
        description="任务名（用于日志与状态展示）；留空则用槽位编号",
        json_schema_extra={"label": "任务名", "hint": "留空则用「播报N」", "placeholder": "每日播报"},
    )
    times: str = Field(
        default="08:00",
        description="每日发送时刻（24 小时制 HH:MM），多个用逗号分隔，如 08:00,20:00；已过去的时刻不补发",
        json_schema_extra={
            "label": "发送时刻",
            "hint": "24 小时制 HH:MM，多个用逗号分隔（如 08:00,20:00）；漏过的时刻不补发",
            "placeholder": "08:00,20:00",
        },
    )
    target_groups: List[str] = Field(
        default_factory=list,
        description="接收播报的 QQ 群号",
        json_schema_extra={
            **_list_field("123456789", "填群号后点 + 添加，可加多个群；群不存在会尝试建会话，失败则跳过"),
            "label": "目标群号",
        },
    )
    target_users: List[str] = Field(
        default_factory=list,
        description="接收播报的 QQ 号（私聊）",
        json_schema_extra={
            **_list_field("10001", "填 QQ 号后点 + 添加，可加多个；与目标群号可以同时配置"),
            "label": "目标 QQ 号",
        },
    )
    keys: str = Field(
        default="",
        description="要播报的指标 id，逗号分隔；留空或 * 表示全部指标",
        json_schema_extra={
            "label": "指标",
            "hint": "留空或 * = 全部指标；也可填 minimax.interval_remaining,minimax.interval_reset",
            "placeholder": "*",
        },
    )
    mode: Literal["image", "template", "llm"] = Field(
        default="image",
        description="发送方式：image=图片（渲染失败自动回退文本）；template=模板文本；llm=LLM 风格化转述",
        json_schema_extra={
            "label": "发送方式",
            "hint": "图片=发渲染好的图片（失败自动回退文本）；模板文本=固定格式；LLM 转述=改写成自然语言",
        },
    )
    condition: str = Field(
        default="",
        description="可选触发条件：支持 > < == >= <= != 与 and/or/not 及括号，如 minimax.interval_remaining<20；留空表示每次都发",
        json_schema_extra={
            "label": "触发条件",
            "hint": "可选。留空=每次都发；也可填 minimax.interval_remaining<20 这类条件（支持 and/or/not 与括号）",
            "placeholder": "minimax.interval_remaining<20",
        },
    )
    content: str = Field(
        default="",
        description="可选：自定义文本模板（占位符同「文本模板」分节）；留空则用全局文本模板",
        json_schema_extra={
            **_textarea_field("自定义内容", "可选。留空则用「文本模板」分节里的模板；支持同样的占位符", rows=4),
        },
    )


class AlertSlotConfig(PluginConfigBase):
    """单个条件警报任务槽位。"""

    enabled: bool = Field(
        default=False,
        description="是否启用此警报任务",
        json_schema_extra={"label": "启用此警报", "hint": "默认关闭；开启前请先填好条件与目标"},
    )
    name: str = Field(
        default="",
        description="任务名（用于日志与状态展示）；留空则用槽位编号",
        json_schema_extra={"label": "任务名", "hint": "留空则用「警报N」", "placeholder": "余额告警"},
    )
    interval_seconds: int = Field(
        default=3600,
        ge=5,
        le=86400,
        description="查询间隔（秒），默认 3600；每次到达间隔即查询一次并判断条件",
        json_schema_extra={
            "label": "查询间隔（秒）",
            "hint": "每隔多久查询一次并判断条件；默认 3600（1 小时），最小 5",
            "placeholder": "3600",
            "step": 60,
        },
    )
    target_groups: List[str] = Field(
        default_factory=list,
        description="接收警报的 QQ 群号",
        json_schema_extra={
            **_list_field("123456789", "填群号后点 + 添加，可加多个群；群不存在会尝试建会话，失败则跳过"),
            "label": "目标群号",
        },
    )
    target_users: List[str] = Field(
        default_factory=list,
        description="接收警报的 QQ 号（私聊）",
        json_schema_extra={
            **_list_field("10001", "填 QQ 号后点 + 添加，可加多个；与目标群号可以同时配置"),
            "label": "目标 QQ 号",
        },
    )
    keys: str = Field(
        default="",
        description="参与判断与播报的指标 id，逗号分隔；留空表示全部指标",
        json_schema_extra={
            "label": "指标",
            "hint": "留空=全部指标；条件里引用的指标必须在这里（或为全部）",
            "placeholder": "minimax.interval_remaining",
        },
    )
    condition: str = Field(
        default="",
        description="触发条件（必填）：支持 > < == >= <= != 与 and/or/not 及括号，如 minimax.interval_remaining<20；留空则永不触发",
        json_schema_extra={
            "label": "触发条件",
            "hint": "必填，留空则永不触发。如 minimax.interval_remaining<20 and minimax.weekly_remaining<50",
            "placeholder": "minimax.interval_remaining<20",
        },
    )
    mode: Literal["llm", "template", "image"] = Field(
        default="llm",
        description="发送方式：llm=LLM 风格化转述；template=模板文本；image=图片",
        json_schema_extra={
            "label": "发送方式",
            "hint": "LLM 转述=改写成自然语言；模板文本=固定格式；图片=发渲染好的图片（失败自动回退文本）",
        },
    )
    content: str = Field(
        default="",
        description="可选：自定义文本模板；留空则用全局文本模板",
        json_schema_extra={
            **_textarea_field("自定义内容", "可选。留空则用「文本模板」分节里的模板；支持同样的占位符", rows=4),
        },
    )


# ────────────────────────── 单例分节 ──────────────────────────


class DisplaySection(PluginConfigBase):
    """数值展示配置。"""

    __ui_label__ = "数值展示"
    __ui_icon__ = "gauge"
    __ui_order__ = 300

    quota_decimals: int = Field(
        default=2,
        ge=0,
        le=8,
        description="计数类数值显示的小数位数；百分比固定显示 1 位小数",
        json_schema_extra={
            "label": "数值小数位",
            "hint": "计数类数值显示几位小数；百分比始终显示 1 位小数",
            "x-widget": "slider",
            "step": 1,
        },
    )
    show_progress: bool = Field(
        default=True,
        description="百分比类指标是否显示进度条",
        json_schema_extra={"label": "显示进度条", "hint": "开启后百分比指标显示为进度条"},
    )
    show_percentage: bool = Field(
        default=True,
        description="百分比类指标是否显示百分比数字；关闭后只显示进度条",
        json_schema_extra={"label": "显示百分比", "hint": "关闭后百分比只显示进度条，不显示数字"},
    )
    progress_bar_width: int = Field(
        default=20,
        ge=5,
        le=50,
        description="进度条字符宽度",
        json_schema_extra={
            "label": "进度条宽度",
            "hint": "进度条由多少个字符组成",
            "x-widget": "slider",
            "step": 1,
        },
    )


class TextTemplateSection(PluginConfigBase):
    """文本模板配置。"""

    __ui_label__ = "文本模板"
    __ui_icon__ = "file-text"
    __ui_order__ = 301

    template: str = Field(
        default=DEFAULT_TEXT_TEMPLATE,
        description="文本模板。可用占位符：{date} 时间、{count} 指标数量、{items} 全部指标多行区块、"
        "{unavailable} 失败清单、{platforms} 平台清单，以及每个指标的 "
        "{指标id}（展示值）、{指标id_raw}（纯数值）、{指标id_name}（显示名）、"
        "{指标id_progress}（百分比）、{指标id_error}（错误信息）。"
        "正文里想显示花括号本身请写成 {{ 与 }}。",
        json_schema_extra={
            **_textarea_field(
                "文本模板",
                "占位符随取随用：{date} {count} {items} {unavailable} {platforms}，"
                "以及每个指标的 {id}/{id_raw}/{id_name}/{id_progress}/{id_error}（id 形如 minimax.interval_remaining）；"
                "正文里显示花括号请写成 {{ 与 }}",
                rows=6,
                placeholder="🍚 饭卡状态（{date}）\n{items}",
            ),
        },
    )


class LlmRewriteSection(PluginConfigBase):
    """LLM 风格化转述配置。"""

    __ui_label__ = "LLM 风格化转述"
    __ui_icon__ = "sparkles"
    __ui_order__ = 302

    provider: Literal["host", "custom"] = Field(
        default="host",
        description="模型来源：host=复用宿主已配置好的模型组；custom=调用下面填写的 [OI] Chat Completions 兼容服务",
        json_schema_extra={
            "label": "模型来源",
            "hint": "host=用宿主模型组（模型与温度由宿主维护）；custom=用下面填写的自定义兼容服务",
        },
    )
    llm_task_name: str = Field(
        default="utils",
        description="模型来源为 host 时复用的宿主模型组（model_config.toml 里的任务配置名，如 utils / replyer / planner）",
        json_schema_extra={
            "label": "宿主模型组",
            "hint": "仅「模型来源=host」时生效：填宿主已分配好的模型组名，例如 utils / replyer / planner",
            "placeholder": "utils",
        },
    )
    api_base_url: str = Field(
        default="",
        description="自定义服务的接口基地址（[OI] Chat Completions 兼容），插件会向「基地址 + /chat/completions」发 POST",
        json_schema_extra={
            "label": "接口地址",
            "hint": "仅「模型来源=custom」时生效：填到 /v1 为止即可，插件自动补 /chat/completions",
            "placeholder": "https://api.example.com/v1",
        },
    )
    api_key: str = Field(
        default="",
        description="自定义服务的 API Key，会以 Authorization: Bearer <key> 发送；留空则不发送该请求头",
        json_schema_extra={
            "label": "API Key",
            "hint": "仅「模型来源=custom」时生效：留空则不带 Authorization 头（适合本地免鉴权服务）",
            "x-widget": "password",
            "placeholder": "sk-…",
        },
    )
    model: str = Field(
        default="",
        description="自定义服务使用的模型名（请求体里的 model 字段）",
        json_schema_extra={
            "label": "模型名",
            "hint": "仅「模型来源=custom」时生效：填服务方要求的模型 ID",
            "placeholder": "deepseek-chat",
        },
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="采样温度（0~2），仅自定义服务生效",
        json_schema_extra={
            "label": "温度",
            "hint": "仅「模型来源=custom」时生效：越低越稳定、越高越发散",
            "x-widget": "slider",
            "step": 0.1,
        },
    )
    extra_body: str = Field(
        default="",
        description='额外的请求体参数（JSON 对象文本），与默认参数合并（同名时以这里为准），例如 {"top_p": 0.9}',
        json_schema_extra={
            **_textarea_field("额外请求参数", '仅「模型来源=custom」时生效：填 JSON 对象文本，例如 {"top_p": 0.9}', rows=3),
        },
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="自定义服务调用失败后的重试次数（0 表示不重试）",
        json_schema_extra={
            "label": "重试次数",
            "hint": "仅「模型来源=custom」时生效：网络错误与 5xx / 429 会重试，其余 4xx 直接失败不重试",
            "placeholder": "2",
            "step": 1,
        },
    )
    timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="单次自定义服务请求的超时时间（秒）",
        json_schema_extra={"label": "超时时间（秒）", "hint": "仅「模型来源=custom」时生效", "placeholder": "60", "step": 5},
    )
    retry_interval_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=60.0,
        description="两次重试之间的等待时间（秒）",
        json_schema_extra={"label": "重试间隔（秒）", "hint": "仅「模型来源=custom」时生效", "placeholder": "1", "step": 0.5},
    )
    use_persona: bool = Field(
        default=True,
        description="开启后自动读取宿主人格（[personality] 人格设定与表达风格、[bot] 昵称）并按模板注入提示词",
        json_schema_extra={"label": "注入宿主人格", "hint": "自动读取宿主的 [personality] 人格与表达风格、[bot] 昵称，拼进提示词"},
    )
    persona_template: str = Field(
        default="",
        description="人格提示词模板，留空则使用内置拼装。可用占位符：{bot_name}、{personality}、{reply_style}",
        json_schema_extra={
            **_textarea_field(
                "人格提示词模板",
                "留空=用内置拼装（昵称/人格/表达风格各一行）；可用占位符 {bot_name}、{personality}、{reply_style}",
                rows=4,
                placeholder="你是{bot_name}。你的人格设定：{personality}。你的表达风格：{reply_style}",
            ),
        },
    )
    persona_extra: str = Field(
        default="",
        description="额外提示词，会追加在人格提示词之后（注入宿主人格关闭时它仍会生效）",
        json_schema_extra={
            **_textarea_field("额外提示词", "追加在人格之后；即使关掉「注入宿主人格」这条也仍然生效", rows=3),
        },
    )
    llm_prompt: str = Field(
        default=DEFAULT_LLM_PROMPT,
        description="转述任务说明（不含数据本体与人格）；人格会拼在它前面，查询数据会附在它后面",
        json_schema_extra={
            **_textarea_field("转述提示词", "只写任务说明即可；系统会自动把「人格」拼在前面、「查询数据」附在后面", rows=5),
        },
    )


class CommandSection(PluginConfigBase):
    """``/饭卡`` 指令配置。"""

    __ui_label__ = "饭卡指令"
    __ui_icon__ = "terminal"
    __ui_order__ = 303

    enabled: bool = Field(
        default=True,
        description="关闭后 /饭卡 不再响应（其他功能不受影响）",
        json_schema_extra={"label": "启用指令", "hint": "关闭后 /饭卡 不再响应，其他功能不受影响"},
    )
    default_query_keys: str = Field(
        default="",
        description="不带指标参数时默认查询的指标 id，多个用逗号分隔；留空表示查询全部指标",
        json_schema_extra={
            "label": "默认指标",
            "hint": "留空=查询全部指标；也可填 minimax.interval_remaining,minimax.interval_reset",
            "placeholder": "minimax.interval_remaining",
        },
    )
    notify_no_permission: bool = Field(
        default=True,
        description="无权限时是否回复提示；关闭后无权限的调用会被静默忽略",
        json_schema_extra={"label": "提示无权限", "hint": "关闭后无权限时完全静默（不发任何消息）"},
    )
    deny_message: str = Field(
        default="你没有权限使用该命令",
        description="无权限时的回复内容；留空则静默拒绝",
        json_schema_extra={"label": "无权限提示", "hint": "「提示无权限」开启时回复的内容；留空则不回复", "placeholder": "你没有权限使用该命令"},
    )
    send_mode: Literal["template", "llm", "image"] = Field(
        default="image",
        description="指令回复的发送方式：template=模板文本，llm=LLM 风格化转述，image=图片（失败自动回退文本）",
        json_schema_extra={
            "label": "发送方式",
            "hint": "模板文本=固定格式；LLM 转述=改写成自然语言；图片=发渲染好的图片，渲染失败自动回退模板文本",
        },
    )


class _PermissionSectionBase(PluginConfigBase):
    """权限分节公共字段（两组权限各自继承一份，互不影响）。"""

    permission_mode: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="对话流名单制度：whitelist=名单外一律不可用；blacklist=名单外一律可用",
        json_schema_extra={
            "label": "名单制度",
            "hint": "白名单（默认）=只有名单内的对话可用；黑名单=只有名单内的对话不可用（想全放开就用黑名单+留空）",
        },
    )
    whitelist_groups: List[str] = Field(
        default_factory=list,
        description="白名单制度下允许使用的群号；与群黑名单冲突时黑名单优先",
        json_schema_extra={**_list_field("123456789", "填群号后点 + 添加；仅在「白名单」制度下生效"), "label": "白名单群号"},
    )
    whitelist_users: List[str] = Field(
        default_factory=list,
        description="命中的 QQ 号始终可用（只绕开对话流名单，仍受放行指标约束）",
        json_schema_extra={**_list_field("10001", "填 QQ 号后点 + 添加；命中的用户始终可用，但仍受放行指标限制"), "label": "白名单 QQ 号"},
    )
    blacklist_groups: List[str] = Field(
        default_factory=list,
        description="命中的群在任何制度下都不可用（优先于群白名单）",
        json_schema_extra={**_list_field("123456789", "填群号后点 + 添加；命中的群在任何制度下都不可用"), "label": "黑名单群号"},
    )
    blacklist_users: List[str] = Field(
        default_factory=list,
        description="命中的 QQ 号始终不可用（最高优先级，高于用户白名单）",
        json_schema_extra={**_list_field("10001", "填 QQ 号后点 + 添加；命中的用户始终不可用（优先级最高）"), "label": "黑名单 QQ 号"},
    )
    allow_keys: List[str] = Field(
        default_factory=list,
        description="全局默认可查询的指标 id；留空表示全部放行",
        json_schema_extra={
            **_list_field("minimax.interval_remaining", "填指标 id 后点 + 添加；留空=全部放行。仅这些指标允许被查询"),
            "label": "放行指标",
        },
    )
    scope_overrides: List[str] = Field(
        default_factory=list,
        description="针对单个对话流覆盖放行指标，格式为分号分隔的「字段=值」，例如 type=群聊;id=123456;keys=minimax.interval_remaining",
        json_schema_extra={
            **_list_field(
                "type=群聊;id=123456;keys=minimax.interval_remaining",
                "格式：分号分隔的「字段=值」。字段：type（群聊/私聊，也支持 群/个人/用户）、id（群号或账号）、"
                "keys（放行指标 id，逗号分隔；* = 全部放行；留空 = 全部禁止）。留空整条=用全局放行指标。",
            ),
            "label": "每对话覆盖",
        },
    )


class ToolPermissionSection(_PermissionSectionBase):
    """第一组权限：LLM 工具 ``query_meal_card`` 的查询权限。"""

    __ui_label__ = "LLM 工具查询权限"
    __ui_icon__ = "shield"
    __ui_order__ = 304


class CommandPermissionSection(_PermissionSectionBase):
    """第二组权限：``/饭卡`` 指令的查询权限。"""

    __ui_label__ = "/饭卡 指令查询权限"
    __ui_icon__ = "shield-check"
    __ui_order__ = 305


class RenderSection(PluginConfigBase):
    """图片渲染配置。"""

    __ui_label__ = "图片渲染"
    __ui_icon__ = "image"
    __ui_order__ = 306

    template_name: str = Field(
        default="simple",
        description="图片模板名称：内置 simple / dark / card，或用户模板目录里新增的模板文件名（不含 .html）",
        json_schema_extra={
            "label": "图片模板",
            "hint": "填 simple / dark / card，或用户模板目录里新增的模板名；内置模板数据相同、只是配色排版不同",
            "placeholder": "simple",
        },
    )
    show_footer: bool = Field(
        default=True,
        description="是否在图片底部显示页脚",
        json_schema_extra={"label": "显示页脚", "hint": "关闭后图片不再显示底部页脚（默认「由麦麦饭卡插件渲染」）"},
    )
    footer_text: str = Field(
        default="由麦麦饭卡插件渲染",
        description="图片底部页脚文案；留空则不显示页脚",
        json_schema_extra={
            "label": "页脚文案",
            "hint": "仅「显示页脚」开启且此处置空时才会隐藏；可自定义成任意文字（如留空则不显示）",
            "placeholder": "由麦麦饭卡插件渲染",
        },
    )
    image_format: Literal["png", "jpeg"] = Field(
        default="png",
        description="图片格式：PNG 无损体积较大，JPEG 体积小、可指定质量",
        json_schema_extra={"label": "图片格式", "hint": "PNG 无损、体积大；JPEG 体积小，可配合下面的「JPEG 质量」调节"},
    )
    jpeg_quality: int = Field(
        default=90,
        ge=1,
        le=100,
        description="JPEG 质量（1~100），仅在「图片格式」为 JPEG 时生效",
        json_schema_extra={"label": "JPEG 质量", "hint": "仅当图片格式为 JPEG 时生效；越低体积越小", "x-widget": "slider", "step": 1},
    )
    resolution_scale: float = Field(
        default=2.0,
        ge=1.0,
        le=4.0,
        description="分辨率等级（设备像素比）：越大越清晰、图片越大、渲染越慢",
        json_schema_extra={"label": "分辨率等级", "hint": "越大越清晰也越慢：2.0 适合手机查看", "x-widget": "slider", "step": 0.5},
    )
    first_round_timeout_ms: int = Field(
        default=15000,
        ge=1000,
        le=60000,
        description="第一轮渲染的单次超时（毫秒）",
        json_schema_extra={"label": "第一轮超时", "hint": "单位毫秒；超过就换下一个字体服务组重试", "placeholder": "15000", "step": 1000},
    )
    second_round_timeout_ms: int = Field(
        default=25000,
        ge=1000,
        le=60000,
        description="第二轮（回退轮）的单次超时（毫秒）",
        json_schema_extra={"label": "第二轮超时", "hint": "单位毫秒；仅在第一轮全部失败后的兜底轮使用", "placeholder": "25000", "step": 1000},
    )
    viewport_width: int = Field(
        default=1000,
        ge=200,
        le=3000,
        description="第一轮渲染的浏览器视口宽度（px）",
        json_schema_extra={"label": "视口宽度", "hint": "第一轮浏览器视口宽度（px）", "placeholder": "1000", "step": 50},
    )
    viewport_height: int = Field(
        default=600,
        ge=200,
        le=4000,
        description="第一轮渲染的浏览器视口高度（px）",
        json_schema_extra={"label": "视口高度", "hint": "主要影响布局换行；截图会按内容自适应高度", "placeholder": "600", "step": 50},
    )
    fallback_viewport_width: int = Field(
        default=900,
        ge=200,
        le=3000,
        description="兜底视口宽度（px），仅第二轮使用",
        json_schema_extra={"label": "兜底视口宽度", "hint": "仅第二轮（回退轮）使用", "placeholder": "900", "step": 50},
    )
    fallback_viewport_height: int = Field(
        default=1200,
        ge=200,
        le=4000,
        description="兜底视口高度（px），仅第二轮使用",
        json_schema_extra={"label": "兜底视口高度", "hint": "仅第二轮使用；长报告建议给大一些", "placeholder": "1200", "step": 50},
    )
    allow_network: bool = Field(
        default=True,
        description="是否允许渲染页面访问外部网络；字体服务地址需要联网，关闭后页面只能用系统字体",
        json_schema_extra={"label": "允许联网渲染", "hint": "字体 CDN 需要联网；关闭后只能用系统字体"},
    )
    font_services: List[str] = Field(
        default=[
            "name=内地;css=https://fonts.loli.net;static=https://gstatic.loli.net",
            "name=海外;css=https://fonts.googleapis.com;static=https://fonts.gstatic.com",
        ],
        description="字体服务组，按顺序作为优先级；每条格式为分号分隔的「字段=值」",
        json_schema_extra={
            **_list_field(
                "name=内地;css=https://fonts.loli.net;static=https://gstatic.loli.net",
                "格式：分号分隔的「字段=值」。字段：name（名称）、css（CSS 基地址）、static（静态字体基地址）。"
                "按列表顺序作为优先级，渲染失败自动换下一组。",
            ),
            "label": "字体服务组",
        },
    )


class LimitsSection(PluginConfigBase):
    """请求上限配置。"""

    __ui_label__ = "请求上限"
    __ui_icon__ = "activity"
    __ui_order__ = 307

    request_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=120.0,
        description="单次查询请求的超时时间（秒）",
        json_schema_extra={"label": "请求超时（秒）", "hint": "单次 HTTP 查询请求最长等待时间", "placeholder": "15", "step": 1},
    )
    max_request_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="查询请求失败后的重试次数（0 表示不重试）",
        json_schema_extra={
            "label": "请求重试次数",
            "hint": "网络错误与 5xx / 429 会重试，其余 4xx 直接失败不重试",
            "placeholder": "2",
            "step": 1,
        },
    )


# ────────────────────────── 槽位类生成 ──────────────────────────


def _make_slot_class(base: type, index: int, label: str, icon: str, order: int) -> type:
    """为第 index 个槽位生成一个 ``base`` 的子类（仅 WebUI 展示元数据不同）。

    用 ``type()`` 生成子类，字段全部继承自基类（不动态新增字段），只设置
    ``__ui_label__`` / ``__ui_icon__`` / ``__ui_order__``。
    """

    return type(
        f"{base.__name__}{index}",
        (base,),
        {"__ui_label__": f"{label} {index}", "__ui_icon__": icon, "__ui_order__": order},
    )


PLATFORM_CLASSES: List[type] = [_make_slot_class(PlatformSlotConfig, i, "平台", "server", 10 + i) for i in range(1, PLATFORM_SLOTS + 1)]
BROADCAST_CLASSES: List[type] = [_make_slot_class(BroadcastSlotConfig, i, "播报", "clock", 100 + i) for i in range(1, BROADCAST_SLOTS + 1)]
ALERT_CLASSES: List[type] = [_make_slot_class(AlertSlotConfig, i, "警报", "bell", 200 + i) for i in range(1, ALERT_SLOTS + 1)]


# 顶层配置：plugin + platform1..N + broadcast1..N + alert1..N + 各单例分节。
# 区块名必须含 plugin：宿主的 extract_plugin_config_version 要求 [plugin].config_version 存在。
_FIELD_DEFS: Dict[str, Any] = {
    "plugin": (GeneralSectionConfig, Field(default_factory=GeneralSectionConfig)),
}
for _i, _cls in enumerate(PLATFORM_CLASSES, start=1):
    _FIELD_DEFS[f"platform{_i}"] = (_cls, Field(default_factory=_cls))
for _i, _cls in enumerate(BROADCAST_CLASSES, start=1):
    _FIELD_DEFS[f"broadcast{_i}"] = (_cls, Field(default_factory=_cls))
for _i, _cls in enumerate(ALERT_CLASSES, start=1):
    _FIELD_DEFS[f"alert{_i}"] = (_cls, Field(default_factory=_cls))
_FIELD_DEFS["display"] = (DisplaySection, Field(default_factory=DisplaySection))
_FIELD_DEFS["text_template"] = (TextTemplateSection, Field(default_factory=TextTemplateSection))
_FIELD_DEFS["llm_rewrite"] = (LlmRewriteSection, Field(default_factory=LlmRewriteSection))
_FIELD_DEFS["command"] = (CommandSection, Field(default_factory=CommandSection))
_FIELD_DEFS["llm_tool_permission"] = (ToolPermissionSection, Field(default_factory=ToolPermissionSection))
_FIELD_DEFS["command_permission"] = (CommandPermissionSection, Field(default_factory=CommandPermissionSection))
_FIELD_DEFS["render"] = (RenderSection, Field(default_factory=RenderSection))
_FIELD_DEFS["limits"] = (LimitsSection, Field(default_factory=LimitsSection))

MealCardConfig = create_model(
    "MealCardConfig",
    __base__=PluginConfigBase,
    **_FIELD_DEFS,
)
MealCardConfig.__ui_label__ = "麦麦饭卡"


# ────────────────────────── 运行时辅助 ──────────────────────────

_CHAT_TYPE_ALIASES = {
    "群聊": "group",
    "群": "group",
    "group": "group",
    "私聊": "private",
    "个人": "private",
    "用户": "private",
    "user": "private",
    "private": "private",
}

_KEY_INVALID_RE = None  # 延迟初始化（见 norm_platform_key）


@dataclass(frozen=True)
class TargetSpec:
    """播报目标。"""

    chat_type: str
    target_id: str


@dataclass(frozen=True)
class BroadcastTask:
    """归一化后的定时播报任务。"""

    name: str
    enabled: bool
    times: Tuple[datetime_time, ...]
    targets: Tuple[TargetSpec, ...]
    keys: Tuple[str, ...]
    send_mode: str
    condition: str
    content: str


@dataclass(frozen=True)
class AlertTask:
    """归一化后的警报任务。"""

    name: str
    enabled: bool
    interval_seconds: int
    targets: Tuple[TargetSpec, ...]
    keys: Tuple[str, ...]
    condition: str
    send_mode: str
    content: str


def norm_platform_key(raw: Any, fallback: str) -> str:
    """规范化平台标识：仅保留字母/数字/下划线/短横线/中文，其余替换为下划线；为空则用 fallback。"""

    import re

    text = str(raw or "").strip()
    if not text:
        return fallback
    cleaned = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", text)
    cleaned = cleaned.strip("_-")
    return cleaned or fallback


def parse_schedule_time(raw: str) -> Optional[datetime_time]:
    """解析 ``HH:MM`` 时刻。"""

    text = str(raw or "").strip()
    if not text or ":" not in text:
        return None
    hour_text, minute_text = text.split(":", 1)
    try:
        hour = int(hour_text.strip())
        minute = int(minute_text.strip())
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime_time(hour=hour, minute=minute)


def parse_keys(raw: str) -> Tuple[str, ...]:
    """解析指标 id 串 ``k1,k2`` 或 ``*``（``*``/空表示全部）。"""

    text = str(raw or "").strip()
    if not text or text == "*":
        return ()
    return tuple(key.strip() for key in text.split(",") if key.strip())


def _targets_from_ids(groups: List[str], users: List[str]) -> Tuple[TargetSpec, ...]:
    """把群号列表与 QQ 号列表转换为目标清单。"""

    targets: List[TargetSpec] = []
    for raw in groups:
        group_id = str(raw or "").strip()
        if group_id:
            targets.append(TargetSpec(chat_type="group", target_id=group_id))
    for raw in users:
        user_id = str(raw or "").strip()
        if user_id:
            targets.append(TargetSpec(chat_type="private", target_id=user_id))
    return tuple(targets)


def platform_slots(config: Any) -> List[Any]:
    """返回当前生效数量内的平台槽位（platform1..platform_count）。"""

    count = _clamp_count(getattr(getattr(config, "plugin", None), "platform_count", 1), PLATFORM_SLOTS)
    return [slot for i in range(1, count + 1) if (slot := getattr(config, f"platform{i}", None)) is not None]


def broadcast_slots(config: Any) -> List[Any]:
    """返回当前生效数量内的播报槽位。"""

    count = _clamp_count(getattr(getattr(config, "plugin", None), "broadcast_count", 1), BROADCAST_SLOTS)
    return [slot for i in range(1, count + 1) if (slot := getattr(config, f"broadcast{i}", None)) is not None]


def alert_slots(config: Any) -> List[Any]:
    """返回当前生效数量内的警报槽位。"""

    count = _clamp_count(getattr(getattr(config, "plugin", None), "alert_count", 1), ALERT_SLOTS)
    return [slot for i in range(1, count + 1) if (slot := getattr(config, f"alert{i}", None)) is not None]


def _clamp_count(raw: Any, generated: int) -> int:
    """把配置里的数量钳制到 ``1..generated``。"""

    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(generated, value))


def to_broadcast_tasks(config: Any) -> List[BroadcastTask]:
    """把播报槽位归一化为 :class:`BroadcastTask` 列表。"""

    tasks: List[BroadcastTask] = []
    for index, slot in enumerate(broadcast_slots(config), start=1):
        name = str(getattr(slot, "name", "") or "").strip() or f"播报{index}"
        times: List[datetime_time] = []
        for token in str(getattr(slot, "times", "") or "").split(","):
            parsed = parse_schedule_time(token)
            if parsed is not None:
                times.append(parsed)
            elif token.strip():
                logger.warning("[meal_card] 播报任务 %s 的时刻格式非法（应为 HH:MM），已忽略：%s", name, token)
        if not times:
            logger.warning("[meal_card] 播报任务 %s 没有可用的发送时刻，已跳过", name)
            continue
        tasks.append(
            BroadcastTask(
                name=name,
                enabled=bool(getattr(slot, "enabled", False)),
                times=tuple(times),
                targets=_targets_from_ids(
                    list(getattr(slot, "target_groups", []) or []),
                    list(getattr(slot, "target_users", []) or []),
                ),
                keys=parse_keys(getattr(slot, "keys", "")),
                send_mode=str(getattr(slot, "mode", "image") or "image").strip().lower(),
                condition=str(getattr(slot, "condition", "") or ""),
                content=str(getattr(slot, "content", "") or ""),
            )
        )
    return tasks


def to_alert_tasks(config: Any) -> List[AlertTask]:
    """把警报槽位归一化为 :class:`AlertTask` 列表。"""

    tasks: List[AlertTask] = []
    for index, slot in enumerate(alert_slots(config), start=1):
        name = str(getattr(slot, "name", "") or "").strip() or f"警报{index}"
        try:
            interval = int(getattr(slot, "interval_seconds", 3600))
        except (TypeError, ValueError):
            interval = 3600
        if interval < 5:
            logger.warning("[meal_card] 警报任务 %s 的间隔过小（%s），已按 3600 秒处理", name, interval)
            interval = 3600
        condition = str(getattr(slot, "condition", "") or "").strip()
        if not condition:
            logger.warning("[meal_card] 警报任务 %s 未设置条件，将永不触发", name)
        tasks.append(
            AlertTask(
                name=name,
                enabled=bool(getattr(slot, "enabled", False)),
                interval_seconds=interval,
                targets=_targets_from_ids(
                    list(getattr(slot, "target_groups", []) or []),
                    list(getattr(slot, "target_users", []) or []),
                ),
                keys=parse_keys(getattr(slot, "keys", "")),
                condition=condition,
                send_mode=str(getattr(slot, "mode", "llm") or "llm").strip().lower(),
                content=str(getattr(slot, "content", "") or ""),
            )
        )
    return tasks


def _parse_named_entry(raw: Any, kind: str, allowed: frozenset) -> Optional[Dict[str, List[str]]]:
    """把一条「字段=值」条目解析为 ``{字段名: [值, ...]}``（仅用于权限覆盖）。"""

    text = str(raw or "").strip()
    if not text:
        return None
    fields: Dict[str, List[str]] = {}
    for chunk in text.split(";"):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            logger.warning("[meal_card] %s 条目片段缺少「=」，已忽略：%s", kind, item)
            continue
        name, value = item.split("=", 1)
        name = name.strip().lower()
        if not name:
            continue
        if name not in allowed:
            logger.warning("[meal_card] %s 条目含未知字段「%s」，已忽略（可用：%s）", kind, name, "、".join(sorted(allowed)))
            continue
        fields.setdefault(name, []).append(value.strip())
    return fields or None


def _first(fields: Dict[str, List[str]], name: str, default: str = "") -> str:
    """取字段的第一个值；不存在时返回默认值。"""

    values = fields.get(name)
    if not values:
        return default
    return values[0]


_OVERRIDE_FIELDS = frozenset({"type", "id", "keys"})


def parse_scope_overrides(items: List[str]) -> Dict[Tuple[str, str], Optional[frozenset]]:
    """解析每对话放行指标覆盖。

    返回值语义：``None`` = 全部放行；``frozenset()`` = 全部禁止；否则为放行指标集合。
    """

    result: Dict[Tuple[str, str], Optional[frozenset]] = {}
    for raw in items:
        fields = _parse_named_entry(raw, "每对话覆盖", _OVERRIDE_FIELDS)
        if fields is None:
            continue
        raw_type = _first(fields, "type")
        chat_type = _CHAT_TYPE_ALIASES.get(raw_type.lower()) or _CHAT_TYPE_ALIASES.get(raw_type)
        target_id = _first(fields, "id")
        if not chat_type or not target_id:
            logger.warning("[meal_card] 每对话覆盖缺少必填字段 type 或 id，已跳过：%s", raw)
            continue
        if "keys" not in fields:
            logger.warning("[meal_card] 每对话覆盖缺少字段 keys，已跳过：%s", raw)
            continue
        raw_keys = _first(fields, "keys").strip()
        if raw_keys == "*":
            allowed: Optional[frozenset] = None
        elif not raw_keys:
            allowed = frozenset()
        else:
            allowed = frozenset(key.strip() for key in raw_keys.split(",") if key.strip())
        result[(chat_type, target_id)] = allowed
    return result
