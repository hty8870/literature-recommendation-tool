"""RFC 8785-style canonical JSON for local content addressing.

M0 implements the subset the contracts need without a third-party JCS
dependency: object keys sort by UTF-16 code units, strings are emitted as
UTF-8 without ASCII escaping, separators are compact, non-finite numbers are
rejected, and finite numbers use ECMAScript-style formatting.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("JCS does not allow NaN or infinity")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    decimal = Decimal(repr(abs(value))).normalize()
    digits = "".join(str(part) for part in decimal.as_tuple().digits).rstrip("0") or "0"
    exponent = decimal.as_tuple().exponent
    point = exponent + len(digits)
    if point <= 0 and point > -6:
        return f"{sign}0.{'0' * -point}{digits}"
    if 0 < point <= 21:
        if point >= len(digits):
            return sign + digits + "0" * (point - len(digits))
        return f"{sign}{digits[:point]}.{digits[point:]}"
    tail = digits[1:]
    mantissa = digits[0] + (f".{tail}" if tail else "")
    return f"{sign}{mantissa}e{'+' if point - 1 >= 0 else '-'}{abs(point - 1)}"


def _dump(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_dump(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be"))
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False) + ":" + _dump(item) for key, item in pairs
            )
            + "}"
        )
    raise TypeError(f"unsupported canonical JSON type: {type(value)!r}")


def canonical_json(value: Any) -> str:
    return _dump(value)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
