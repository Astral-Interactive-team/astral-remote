# Bounded login retry with exponential backoff.
# discord.py reconnects by itself after login, but initial login (bad
# network, outage) retries here. Fatal errors such as an invalid token
# rethrow immediately. Ported from src/agent/connection.js.

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable

from astral_remote.shared.logging_utils import info, warn

BASE_DELAY_MS = 1000
MAX_DELAY_MS = 60000
MAX_ATTEMPTS = 6


def is_fatal_auth_error(err: Any) -> bool:
    """Match invalid-token rejections on error shape only. Never includes token text."""
    if err is None or not isinstance(err, BaseException):
        if not isinstance(err, dict) and not hasattr(err, "code"):
            # Accept duck-typed error objects used by discord.py.
            pass
        if err is None:
            return False
    code = getattr(err, "code", None)
    if code in ("TokenInvalid", 401):
        return True
    # discord.py raises LoginFailure / PrivilegedIntentsRequired with status/code.
    name = type(err).__name__ if isinstance(err, BaseException) else ""
    if name in ("LoginFailure", "PrivilegedIntentsRequired"):
        return True
    return "invalid token" in str(getattr(err, "message", getattr(err, "args", [""])[0] if getattr(err, "args", None) else "")).lower() or "invalid token" in str(err).lower()


async def login_with_backoff(
    login_fn: Callable[[], Awaitable[Any]],
    attempts: int = MAX_ATTEMPTS,
    is_fatal: Callable[[Any], bool] = is_fatal_auth_error,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """Run login_fn until success or attempts run out. Fatal auth errors rethrow at once."""
    last_error: Any = None
    for i in range(attempts):
        try:
            return await login_fn()
        except Exception as err:  # noqa: BLE001 - rethrown after retries
            last_error = err
            if is_fatal(err):
                warn("Authentication rejected. Not retrying.")
                raise
            if i == attempts - 1:
                break
            delay = min(BASE_DELAY_MS * (2**i), MAX_DELAY_MS)
            jitter = random.randint(0, 499)
            warn(f"Login attempt {i + 1} failed. Retrying soon.")
            await sleep((delay + jitter) / 1000.0)
    info("Login retries exhausted.")
    raise last_error


def backoff_delay(index: int) -> int:
    """Compute backoff delay for an attempt index. Exported for tests."""
    return min(BASE_DELAY_MS * (2**index), MAX_DELAY_MS)
