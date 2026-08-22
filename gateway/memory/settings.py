from __future__ import annotations

import os


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def env_string(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()

def env_int(name: str, default: int, min_val: int | None = None, max_val: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = int(raw.strip())
        if min_val is not None:
            val = max(min_val, val)
        if max_val is not None:
            val = min(max_val, val)
        return val
    except ValueError:
        return default
