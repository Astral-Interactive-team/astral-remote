# Embedded payload access for the production agent.
# The builder generates `_embedded_payload.py` inside an isolated staging
# package copy; PyInstaller bundles it as code. At runtime the agent
# imports it lazily so --version never touches token material.
# Token shape and config validation mirror src/agent/payload.js.

from __future__ import annotations

import re
from typing import Any

from astral_remote.shared.config import require_valid_config

PAYLOAD_VERSION = 2

_TOKEN_RE = re.compile(r"^[\w\-]{10,}\.[\w\-]{5,}\.[\w\-]{10,}$")


def is_plausible_bot_token(token: Any) -> bool:
    """Offline bot-token shape check: three dot-separated parts. Heuristic only."""
    if not isinstance(token, str):
        return False
    return _TOKEN_RE.match(token.strip()) is not None


def load_embedded_payload() -> dict[str, Any]:
    """Import the generated payload module. Raises BuildError-style Error when absent.

    Kept lazy so diagnostic commands can run without importing secrets.
    """
    try:
        from astral_remote import _embedded_payload as embedded  # type: ignore
    except ImportError as exc:
        raise FileNotFoundError("No embedded payload. Rebuild the EXE with the current builder.") from exc
    token = getattr(embedded, "TOKEN", None)
    config = getattr(embedded, "CONFIG", None)
    version = getattr(embedded, "PAYLOAD_V", None)
    if version != PAYLOAD_VERSION:
        raise ValueError("Payload version unsupported.")
    if not is_plausible_bot_token(token):
        raise ValueError("Embedded payload holds no bot token. Rebuild the EXE with a fresh token.")
    return {"token": token.strip(), "config": require_valid_config(config)}
