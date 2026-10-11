"""警报 / 播报触发条件的表达式解析与求值。

支持：
- 比较运算：``>`` ``<`` ``==`` ``>=`` ``<=`` ``!=``，形式为 ``查询键 运算符 数字``；
- 逻辑运算：``and`` / ``or`` / ``not``；
- 括号分组。

示例：``remain < 10 and used >= 80``、``not (remain > 100) or usage == 0``。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

_LOGICAL_KEYWORDS = {"and", "or", "not"}
_COMPARISON_OPERATORS = {">", "<", "==", ">=", "<=", "!="}


class ConditionError(ValueError):
    """条件表达式非法或求值失败。"""


@dataclass(frozen=True)
class _Token:
    kind: str  # num / ident / op / kw / lparen / rparen
    text: str


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _tokenize(expression: str) -> List[_Token]:
    """把表达式切分为记号序列。"""

    tokens: List[_Token] = []
    index = 0
    length = len(expression)
    while index < length:
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char == "(":
            tokens.append(_Token("lparen", char))
            index += 1
            continue
        if char == ")":
            tokens.append(_Token("rparen", char))
            index += 1
            continue
        if char in "<>=!":
            two = expression[index : index + 2]
            if two in _COMPARISON_OPERATORS:
                tokens.append(_Token("op", two))
                index += 2
                continue
            if char in "<>":
                tokens.append(_Token("op", char))
                index += 1
                continue
            raise ConditionError(f"非法运算符：{two}")

        end = index
        while end < length and not expression[end].isspace() and expression[end] not in "()<>=!":
            end += 1
        word = expression[index:end]
        index = end
        lowered = word.lower()
        if lowered in _LOGICAL_KEYWORDS:
            tokens.append(_Token("kw", lowered))
        elif _is_number(word):
            tokens.append(_Token("num", word))
        else:
            tokens.append(_Token("ident", word))
    return tokens


class _Parser:
    """递归下降解析器。"""

    def __init__(self, tokens: List[_Token], values: Dict[str, float]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._values = values

    def parse(self) -> bool:
        if not self._tokens:
            raise ConditionError("条件为空")
        value = self._parse_or()
        if self._pos != len(self._tokens):
            raise ConditionError(f"条件尾部有多余内容：{self._tokens[self._pos].text}")
        return value

    def _peek(self) -> Optional[_Token]:
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None

    def _match_keyword(self, keyword: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "kw" and token.text == keyword:
            self._pos += 1
            return True
        return False

    def _parse_or(self) -> bool:
        value = self._parse_and()
        while self._match_keyword("or"):
            right = self._parse_and()
            value = value or right
        return value

    def _parse_and(self) -> bool:
        value = self._parse_not()
        while self._match_keyword("and"):
            right = self._parse_not()
            value = value and right
        return value

    def _parse_not(self) -> bool:
        if self._match_keyword("not"):
            return not self._parse_not()
        return self._parse_comparison()

    def _parse_comparison(self) -> bool:
        token = self._peek()
        if token is None:
            raise ConditionError("条件表达式不完整")
        if token.kind == "lparen":
            self._pos += 1
            value = self._parse_or()
            closing = self._peek()
            if closing is None or closing.kind != "rparen":
                raise ConditionError("括号未闭合")
            self._pos += 1
            return value
        if token.kind != "ident":
            raise ConditionError(f"此处应为查询键，实际为：{token.text}")
        self._pos += 1
        name = token.text

        operator_token = self._peek()
        if operator_token is None or operator_token.kind != "op":
            raise ConditionError(f"查询键 {name} 后缺少比较运算符")
        self._pos += 1
        operator = operator_token.text

        number_token = self._peek()
        if number_token is None or number_token.kind != "num":
            raise ConditionError(f"比较运算符 {operator} 后缺少数字")
        self._pos += 1
        expected = float(number_token.text)

        actual = self._values.get(name)
        if actual is None:
            raise ConditionError(f"查询键 {name} 没有可用数值")
        return _compare(actual, operator, expected)


def _compare(actual: float, operator: str, expected: float) -> bool:
    """执行一次比较。"""

    if operator == ">":
        return actual > expected
    if operator == "<":
        return actual < expected
    if operator == ">=":
        return actual >= expected
    if operator == "<=":
        return actual <= expected
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    raise ConditionError(f"不支持的比较运算符：{operator}")


def evaluate(expression: str, values: Dict[str, float]) -> Tuple[bool, str]:
    """求值条件表达式。

    Args:
        expression: 条件表达式文本；空字符串表示无条件。
        values: ``{查询键: 数值}``。

    Returns:
        Tuple[bool, str]: ``(是否满足, 错误信息)``。表达式为空时返回 ``(True, "")``；
        表达式非法或求值失败时返回 ``(False, 原因)``（不抛异常）。
    """

    text = str(expression or "").strip()
    if not text:
        return True, ""
    try:
        tokens = _tokenize(text)
        parser = _Parser(tokens, values)
        return parser.parse(), ""
    except ConditionError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - 兜底
        return False, f"条件求值异常：{exc}"
