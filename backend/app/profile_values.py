from __future__ import annotations

from decimal import Decimal, InvalidOperation


def cpu_limit_to_millicores(value: object) -> int:
    """Convert a profile CPU value to exact integer millicores.

    Decimal(str(...)) avoids binary-float accumulation in admission decisions. The
    profile contract deliberately rejects precision below one millicore.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("cpu_limit must be numeric")
    try:
        cores = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("cpu_limit must be numeric") from exc
    if not cores.is_finite() or cores <= 0:
        raise ValueError("cpu_limit must be finite and positive")
    millicores = cores * 1000
    integral = millicores.to_integral_value()
    if millicores != integral:
        raise ValueError("cpu_limit precision cannot be smaller than one millicore")
    return int(integral)
