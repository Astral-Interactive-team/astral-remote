# Shared per-user path resolution for the Windows agent.
# Single source for the %LOCALAPPDATA%\AstralRemote directory so the error
# log and the installation identity/state file always resolve to the same
# location. Falls back to the temp directory when LocalAppData is absent.
# The pre-rename directory name is exposed so identity.py can adopt the
# state file left behind by an older installation instead of forgetting
# its managed category and creating a duplicate one. Contains no secret
# material and performs no I/O.

from __future__ import annotations

import os
from pathlib import Path

STATE_DIRECTORY = "AstralRemote"
LEGACY_STATE_DIRECTORY = "AstralRelay"


def _base_dir(env=None) -> Path | None:
    """Resolve the per-user base directory (LocalAppData, temp fallback)."""
    source = env if env is not None else os.environ
    base = source.get("LOCALAPPDATA") or source.get("TEMP") or source.get("TMP")
    return Path(base) if base else None


def state_dir(env=None) -> Path | None:
    """Resolve the per-user AstralRemote directory (LocalAppData, temp fallback)."""
    base = _base_dir(env)
    return base / STATE_DIRECTORY if base else None


def legacy_state_dir(env=None) -> Path | None:
    """Resolve the pre-rename AstralRelay directory, or None when unavailable."""
    base = _base_dir(env)
    return base / LEGACY_STATE_DIRECTORY if base else None
