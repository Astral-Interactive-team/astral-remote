# Build verification helpers: size floor, PE subsystem check, and
# subprocess smoke checks for the packaged EXE. All diagnostics are
# secret-free; exit codes are the primary signal because windowed
# executables may have no usable standard streams.

from __future__ import annotations

import os
import subprocess
import tempfile

from astral_remote.builder.subsystem import IMAGE_SUBSYSTEM_WINDOWS_GUI, read_subsystem
from astral_remote.shared.errors import BuildError

MIN_EXE_BYTES = 10 * 1024 * 1024


def check_size(exe_path: str, minimum: int = MIN_EXE_BYTES) -> int:
    """Assert the output meets the size floor. Returns the size in bytes."""
    size = os.path.getsize(exe_path)
    if size < minimum:
        raise BuildError(f"Output suspiciously small ({size} bytes). Build incomplete.")
    return size


def check_gui_subsystem(exe_path: str) -> int:
    """Assert the output uses the GUI subsystem. Returns the subsystem value."""
    with open(exe_path, "rb") as handle:
        value = read_subsystem(handle.read())
    if value is None:
        raise BuildError("Not a 64-bit Windows executable.")
    if value != IMAGE_SUBSYSTEM_WINDOWS_GUI:
        raise BuildError(f"Expected GUI subsystem, found {value}.")
    return value


def smoke_test(exe_path: str, timeout_s: float = 120.0) -> str:
    """Run EXE --version with piped streams. Proves the binary starts standalone."""
    try:
        proc = subprocess.run(
            [exe_path, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, shell=False, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"Built EXE failed to start: {exc}") from exc
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or "AstralRemote-Agent" not in out:
        raise BuildError("Built EXE --version check failed.")
    return out.strip().splitlines()[0] if out.strip() else "AstralRemote-Agent"


def self_test(exe_path: str, timeout_s: float = 120.0) -> dict:
    """Run EXE --self-test with a restricted parent-created report file."""
    fd, report = tempfile.mkstemp(prefix="remote-selftest-", suffix=".json")
    os.close(fd)
    try:
        with open(report, "wb") as handle:
            handle.truncate(0)
        proc = subprocess.run(
            [exe_path, "--self-test", "--self-test-report", report],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, shell=False, stdin=subprocess.DEVNULL,
        )
        import json

        try:
            with open(report, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8") or "{}")
        except (OSError, ValueError):
            data = {}
        return {"returncode": proc.returncode, "report": data,
                "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"Built EXE self-test failed to start: {exc}") from exc
    finally:
        try:
            os.unlink(report)
        except OSError:
            pass
