"""两组查询权限：LLM 工具 与 /饭卡 指令。

两组权限字段与判定完全一致、互相独立（改一组不影响另一组）：
1. 本地调试终端（``is_local_operator``，仅指令有该身份）始终放行；
2. 用户黑名单命中 → 始终拒绝；
3. 用户白名单命中 → 放行「名单层」（仍受放行指标约束）；
4. 群黑名单命中 → 拒绝；
5. ``blacklist`` 制度 → 未命中黑名单即放行；``whitelist`` 制度 → 群聊需群号在白名单，
   私聊无群可判，必须命中用户白名单；
6. 名单通过后解析放行指标集合：命中该对话的覆盖项 → 用其集合；否则用全局放行指标。
"""

from typing import List, Optional, Sequence, Tuple

import logging

from .config import parse_scope_overrides

logger = logging.getLogger(__name__)


def _as_str_set(values: Sequence[str]) -> set:
    return {str(value).strip() for value in values if str(value).strip()}


def check_conversation_access(
    section,
    *,
    group_id: str = "",
    user_id: str = "",
    is_local_operator: bool = False,
) -> bool:
    """判断该对话是否通过名单层判定。"""

    if is_local_operator:
        return True

    normalized_user = str(user_id or "").strip()
    normalized_group = str(group_id or "").strip()

    blacklist_users = _as_str_set(section.blacklist_users)
    whitelist_users = _as_str_set(section.whitelist_users)
    blacklist_groups = _as_str_set(section.blacklist_groups)
    whitelist_groups = _as_str_set(section.whitelist_groups)

    if normalized_user and normalized_user in blacklist_users:
        return False
    if normalized_user and normalized_user in whitelist_users:
        return True

    if normalized_group:
        if normalized_group in blacklist_groups:
            return False
        if str(section.permission_mode).strip().lower() == "blacklist":
            return True
        return normalized_group in whitelist_groups

    # 私聊（没有群号）
    if str(section.permission_mode).strip().lower() == "blacklist":
        return True
    return False


def resolve_allowed_keys(section, *, group_id: str = "", user_id: str = "") -> Optional[frozenset]:
    """解析该对话放行的查询键集合。

    Returns:
        Optional[frozenset]: ``None`` 表示全部放行；``frozenset()`` 表示全部禁止；
        否则为放行指标集合。
    """

    normalized_group = str(group_id or "").strip()
    normalized_user = str(user_id or "").strip()
    chat_type = "group" if normalized_group else "private"
    target_id = normalized_group or normalized_user

    overrides = parse_scope_overrides(section.scope_overrides)
    if target_id and (chat_type, target_id) in overrides:
        return overrides[(chat_type, target_id)]

    global_keys = _as_str_set(section.allow_keys)
    if not global_keys:
        return None
    return frozenset(global_keys)


def filter_requested_keys(
    section,
    *,
    group_id: str = "",
    user_id: str = "",
    requested: Sequence[str] = (),
) -> Tuple[List[str], List[str]]:
    """按放行集合过滤请求的查询键。

    Returns:
        Tuple[List[str], List[str]]: ``(允许的键, 被拒绝的键)``。
    """

    allowed = resolve_allowed_keys(section, group_id=group_id, user_id=user_id)
    allowed_keys: List[str] = []
    denied_keys: List[str] = []
    for key in requested:
        normalized = str(key or "").strip()
        if not normalized:
            continue
        if allowed is None or normalized in allowed:
            allowed_keys.append(normalized)
        else:
            denied_keys.append(normalized)
    return allowed_keys, denied_keys


def describe_allowed_keys(section, *, group_id: str = "", user_id: str = "") -> str:
    """返回放行查询键的中文描述（用于日志与提示）。"""

    allowed = resolve_allowed_keys(section, group_id=group_id, user_id=user_id)
    if allowed is None:
        return "全部查询项"
    if not allowed:
        return "无（全部禁止）"
    return "、".join(sorted(allowed))
