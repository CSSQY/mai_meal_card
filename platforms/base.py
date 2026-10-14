"""平台适配器接口与通用数据类。

新增平台只需：
1. 在 ``platforms/`` 下新建 ``<平台>.py``，实现 :class:`PlatformAdapter`；
2. 在 ``platforms/__init__.py`` 中注册该适配器。

适配器只与「平台槽位配置对象」打交道（见 ``config.PlatformSlotConfig``），
其余模块（查询引擎、权限、调度、模板、渲染）只依赖本文件定义的数据结构，
不感知任何具体平台的接口细节。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

# 指标类型
KIND_PERCENT = "percent"
"""百分比指标（0~100，语义为「剩余」）。"""
KIND_DURATION = "duration"
"""时长指标（数值单位为毫秒）。"""
KIND_COUNT = "count"
"""计数/数值指标。"""
KIND_TEXT = "text"
"""纯文本指标（如原始响应摘要），数值为空。"""

ALL_KINDS: Tuple[str, ...] = (KIND_PERCENT, KIND_DURATION, KIND_COUNT, KIND_TEXT)


@dataclass(frozen=True)
class MetricValue:
    """适配器产出的单个指标。

    适配器只负责「取值」与「命名」；``percent`` / ``count`` / ``duration`` 的展示串
    由查询引擎按 ``[display]`` 配置统一格式化，``text`` 类型则直接使用适配器给出的
    ``display``。
    """

    id: str
    """指标标识（不含平台前缀），如 ``interval_remaining``。"""

    name: str
    """中文显示名。"""

    kind: str
    """指标类型，取值见 ``KIND_*``。"""

    platform_key: str = ""
    """所属平台的标识（配置里的「平台标识」），用作指标 id 前缀。"""

    platform_name: str = ""
    """所属平台的显示名称（用于展示）。"""

    unit: str = ""
    """展示单位（如 美元 / 积分）；仅 count 类型使用。"""

    value: Optional[float] = None
    """数值：percent 为 0~100，duration 为毫秒，count 为原始数值，text 为 None。"""

    display: str = ""
    """展示串（text 类型由适配器提供；其余类型由查询引擎覆盖）。"""

    progress: Optional[float] = None
    """进度 0~1（percent 类型使用）。"""

    ok: bool = True
    """本次取值是否成功。"""

    error: str = ""
    """失败原因（成功时为空）。"""

    @property
    def qualified_id(self) -> str:
        """带平台前缀的完整指标 id（用于 keys / 占位符 / 权限）。"""

        return f"{self.platform_key}.{self.id}" if self.platform_key else self.id


class PlatformAdapter(ABC):
    """平台适配器接口。"""

    id: str = ""
    """平台类型标识（配置里「平台类型」的取值，如 ``minimax`` / ``custom``）。"""

    display_name: str = ""
    """平台类型的中文显示名（槽位未填显示名称时用它）。"""

    @abstractmethod
    def is_configured(self, slot: Any) -> Tuple[bool, str]:
        """判断该平台槽位是否已配置完整。

        Returns:
            Tuple[bool, str]: ``(是否已配置完整, 未配置原因)``。
        """

    @abstractmethod
    def metric_ids(self) -> List[str]:
        """返回本平台提供的指标 id 列表（不含平台前缀）。

        用于错误提示与文档；动态指标（如按模型拆分）可不列入。
        """

    @abstractmethod
    def describe(self, slot: Any) -> str:
        """返回该平台槽位的中文描述（用于报告与日志）。"""

    @abstractmethod
    async def fetch(self, slot: Any, *, timeout: float, retries: int) -> List[MetricValue]:
        """执行查询并返回指标列表。

        约定：**不抛异常**。网络/协议/解析失败时返回 ``ok=False`` 的指标并填写 ``error``。
        """


def format_duration(ms: Optional[float]) -> str:
    """把毫秒格式化为中文时长（如「2 小时 30 分钟」）。"""

    if ms is None:
        return ""
    total_seconds = max(int(ms) // 1000, 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes > 0:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


# ────────────────────────── raw 展示脱敏 ──────────────────────────

_SENSITIVE_KEY_HINTS: Tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "session",
)
"""键名包含这些片段时，其值在 raw 展示中被替换为掩码（启发式，仅用于展示）。"""

_SENSITIVE_MASK = "***"
_MAX_MASK_DEPTH = 6


def mask_sensitive(value: Any, *, _depth: int = 0) -> Any:
    """递归把键名含敏感词的值替换为掩码，仅用于 ``raw`` 展示。

    适配器产出的 ``raw`` 是平台响应体摘要；正常情况不含密钥，但若响应体恰好回显了
    密钥字段，直接展示会给模型与群友。这里按键名做启发式脱敏作为兜底，
    **不改变任何业务取值**（数值提取始终使用原始响应）。
    """

    if _depth > _MAX_MASK_DEPTH:
        return value
    if isinstance(value, dict):
        masked: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(hint in lowered for hint in _SENSITIVE_KEY_HINTS):
                masked[key] = _SENSITIVE_MASK
            else:
                masked[key] = mask_sensitive(raw_value, _depth=_depth + 1)
        return masked
    if isinstance(value, list):
        return [mask_sensitive(item, _depth=_depth + 1) for item in value]
    return value
