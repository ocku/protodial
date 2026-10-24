"""Shared strict JSON decoding helpers."""

from __future__ import annotations

import json
import math
from typing import cast


class NonFiniteNumber(ValueError):
    """A numeric token which cannot be represented as a finite float."""

    def __init__(self, token: str, *, may_extend: bool = False) -> None:
        super().__init__(token)
        self.token = token
        self.may_extend = may_extend


class DuplicateObjectKey(ValueError):
    """A JSON object key which occurs more than once."""


def decode_json_float(value: str) -> float:
    """Decode a JSON float while rejecting overflow to infinity."""
    number = float(value)
    if not math.isfinite(number):
        raise NonFiniteNumber(value, may_extend=True)
    return number


def reject_json_constant(value: str) -> object:
    """Reject JavaScript numeric constants which are not valid JSON."""
    raise NonFiniteNumber(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object without silently replacing duplicate members."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateObjectKey(key)
        result[key] = value
    return result


def strict_json_loads(value: str) -> object:
    """Decode ordinary JSON without lossy numbers or duplicate objects."""
    return cast(
        object,
        json.loads(
            value,
            parse_float=decode_json_float,
            parse_constant=reject_json_constant,
            object_pairs_hook=_unique_object,
        ),
    )
