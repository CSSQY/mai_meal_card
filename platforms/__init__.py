"""平台适配器注册表。

新增平台：实现 :class:`~.base.PlatformAdapter` 后，在本文件末尾调用 :func:`register` 注册，
并在 ``config.PlatformSlotConfig.adapter`` 的 ``Literal`` 中加入该平台 id，其余模块无需改动。
"""

from typing import Dict, List, Optional

from .base import MetricValue, PlatformAdapter
from .custom import CustomAdapter
from .deepseek import DeepSeekAdapter
from .minimax import MiniMaxAdapter
from .volcengine import VolcengineAdapter

_REGISTRY: Dict[str, PlatformAdapter] = {}


def register(adapter: PlatformAdapter) -> PlatformAdapter:
    """注册一个平台适配器（同 id 覆盖）。"""

    _REGISTRY[adapter.id] = adapter
    return adapter


def get_platform(platform_id: str) -> Optional[PlatformAdapter]:
    """按平台类型标识获取适配器；不存在时返回 ``None``。"""

    return _REGISTRY.get(str(platform_id or "").strip())


def list_platforms() -> List[str]:
    """返回已注册的平台类型标识列表。"""

    return list(_REGISTRY.keys())


# ── 内置平台注册 ──
register(MiniMaxAdapter())
register(DeepSeekAdapter())
register(VolcengineAdapter())
register(CustomAdapter())

__all__ = [
    "MetricValue",
    "PlatformAdapter",
    "register",
    "get_platform",
    "list_platforms",
]
