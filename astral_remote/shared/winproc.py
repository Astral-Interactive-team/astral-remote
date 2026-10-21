# Windowless child-process creation flags for the GUI-subsystem Agent.
# The production Agent is packaged with PyInstaller --noconsole and its PE
# subsystem is forced to GUI, so the Agent itself owns no console. When
# such a process starts a console child (powershell.exe, schtasks.exe,
# taskkill.exe) Windows allocates a brand-new console window for that
# child unless the child is created with CREATE_NO_WINDOW. That new
# window is the black window that flashes while the Agent collects
# information or checks its own startup registration.
# This module centralizes the flag so every helper process Astral Remote
# starts stays invisible. It deliberately changes nothing else: callers
# keep their own stdout/stderr piping, timeouts, shell=False, and error
# handling. Programs Astral Remote launches on purpose for the user (for
# example the `!pc open` target) do not use this helper.

from __future__ import annotations

import sys

# Windows creation flag that runs a child without any console window.
# Zero on other platforms, where the flag does not exist.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def hidden_kwargs(**kwargs) -> dict:
    """Return subprocess keyword arguments that hide the child's window.

    The returned mapping is the caller's own keyword arguments with
    CREATE_NO_WINDOW merged into `creationflags`, so stdout/stderr,
    timeout, and shell settings are preserved untouched. On platforms
    without the flag the arguments are returned unchanged.
    """
    merged = dict(kwargs)
    if not CREATE_NO_WINDOW:
        return merged
    merged["creationflags"] = int(merged.get("creationflags") or 0) | CREATE_NO_WINDOW
    return merged
