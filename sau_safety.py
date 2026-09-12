from __future__ import annotations

import os
from functools import wraps


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def external_actions_enabled() -> bool:
    """Return true only when the legacy publishing surface is explicitly enabled."""
    return os.getenv("SAU_ENABLE_EXTERNAL_ACTIONS", "").strip().lower() in TRUE_VALUES


def require_external_actions_enabled() -> None:
    if not external_actions_enabled():
        raise RuntimeError(
            "External publishing is disabled by default. "
            "Set SAU_ENABLE_EXTERNAL_ACTIONS=true only inside an approved execution boundary."
        )


def external_action_guard(function):
    """Fail before browser/account work when an upload wrapper is not enabled."""

    @wraps(function)
    async def guarded(*args, **kwargs):
        require_external_actions_enabled()
        return await function(*args, **kwargs)

    return guarded
