from __future__ import annotations


KERNEL_IDLE_TIMEOUT_DEFAULT_SECONDS = 60 * 60
KERNEL_IDLE_TIMEOUT_MIN_SECONDS = 5 * 60
KERNEL_IDLE_TIMEOUT_MAX_SECONDS = 7 * 24 * 60 * 60
KERNEL_IDLE_TIMEOUT_STEP_SECONDS = 60


def kernel_idle_timeout_is_valid(value: object) -> bool:
    """Return whether a value is a canonical managed kernel-idle timeout."""

    return type(value) is int and (
        value == 0
        or (
            KERNEL_IDLE_TIMEOUT_MIN_SECONDS <= value <= KERNEL_IDLE_TIMEOUT_MAX_SECONDS
            and value % KERNEL_IDLE_TIMEOUT_STEP_SECONDS == 0
        )
    )


def validate_kernel_idle_timeout(value: object) -> int:
    if not kernel_idle_timeout_is_valid(value):
        raise ValueError(
            "kernel idle timeout must be 0 or a whole-minute value between "
            f"{KERNEL_IDLE_TIMEOUT_MIN_SECONDS} and "
            f"{KERNEL_IDLE_TIMEOUT_MAX_SECONDS} seconds"
        )
    assert isinstance(value, int)
    return value
