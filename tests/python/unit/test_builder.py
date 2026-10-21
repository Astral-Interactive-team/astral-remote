# Unit tests for builder codegen validation, staging isolation, and
# atomic publish behavior. Never runs PyInstaller here.

from pathlib import Path

import pytest

import astral_remote.builder.build as build_mod
from astral_remote.builder.build import archive_stale_output, resolve_build_token
from astral_remote.shared.errors import BuildError
from tests.python.unit.test_payload import TOKEN_A


class _FakePipe:
    def __init__(self):
        import io

        self._buf = io.StringIO()

    def isatty(self):
        return False

    def write(self, text):
        return self._buf.write(text)

    def flush(self):
        pass

    def getvalue(self):
        return self._buf.getvalue()


def test_sanitize_exe_name():
    from astral_remote.builder.build import DEFAULT_EXE_NAME, sanitize_exe_name

    assert sanitize_exe_name("") == DEFAULT_EXE_NAME
    assert sanitize_exe_name(None) == DEFAULT_EXE_NAME
    assert sanitize_exe_name("  ") == DEFAULT_EXE_NAME
    assert sanitize_exe_name("MyAgent") == "MyAgent.exe"
    assert sanitize_exe_name("MyAgent.EXE") == "MyAgent.EXE"
    for bad in ("a/b.exe", "a\\b.exe", "C:x.exe", "..\\x.exe", 'a"b.exe', "a<b.exe", "x" * 101):
        with pytest.raises(BuildError):
            sanitize_exe_name(bad)


def test_prompt_exe_name_default_and_invalid(monkeypatch, capsys):
    import io
    import sys as sys_mod

    from astral_remote.builder import build as build_mod

    class Answer(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(sys_mod, "stdin", Answer(""))
    assert build_mod.prompt_exe_name() == build_mod.DEFAULT_EXE_NAME
    monkeypatch.setattr(sys_mod, "stdin", Answer("a/b\n"))
    assert build_mod.prompt_exe_name() == build_mod.DEFAULT_EXE_NAME
    assert "Invalid name" in capsys.readouterr().out
    monkeypatch.setattr(sys_mod, "stdin", Answer("Custom\n"))
    assert build_mod.prompt_exe_name() == "Custom.exe"


def test_prompt_exe_name_is_themed_on_tty(monkeypatch):
    import io
    import sys as sys_mod

    from astral_remote.builder import build as build_mod

    class Tty(io.StringIO):
        def isatty(self):
            return True

    class Answer(io.StringIO):
        def isatty(self):
            return False

    out = Tty()
    monkeypatch.setattr(sys_mod, "stdout", out)
    monkeypatch.setattr(sys_mod, "stdin", Answer("Custom\n"))
    assert build_mod.prompt_exe_name() == "Custom.exe"
    written = out.getvalue()
    assert "\x1b[" in written  # purple ANSI styling applied
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", written)
    assert plain == f"[Output EXE name] (default: {build_mod.DEFAULT_EXE_NAME}): "


def test_resolve_token_validates_before_work():
    calls = []
    with pytest.raises(BuildError):
        resolve_build_token(token="junk", prompt_token=lambda: calls.append(1) or "x")
    assert calls == []
    assert resolve_build_token(token="  " + TOKEN_A + " ", prompt_token=lambda: (_ for _ in ()).throw(AssertionError())) == TOKEN_A


def test_prompt_bool_parses_answers(monkeypatch):
    import io

    def answer(text):
        monkeypatch.setattr(build_mod.sys, "stdin", io.StringIO(text))
        return build_mod.prompt_bool("Test", default=True)

    assert answer("y\n") is True
    assert answer("YES\n") is True
    assert answer("n\n") is False
    assert answer("no\n") is False
    assert answer("\n") is True
    assert answer("maybe\n") is True


def test_prompt_bool_empty_keeps_false_default(monkeypatch):
    import io

    monkeypatch.setattr(build_mod.sys, "stdin", io.StringIO("\n"))
    assert build_mod.prompt_bool("Test", default=False) is False


def test_staging_copy_excludes_real_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "SRC_PACKAGE", Path("astral_remote"))
    monkeypatch.setattr(build_mod, "BUILD_TMP", tmp_path)
    stage = build_mod._stage_package()
    try:
        assert (stage / "pkg" / "run_agent.py").is_file()
        assert not (stage / "pkg" / "astral_remote" / "_embedded_payload.py").exists()
        assert Path("astral_remote/_embedded_payload.py").exists() is False
    finally:
        build_mod._cleanup_tracked()


def test_ensure_pyinstaller_reports_missing(monkeypatch):
    monkeypatch.setattr(build_mod.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(BuildError) as excinfo:
        build_mod._ensure_pyinstaller()
    assert "requirements.txt" in str(excinfo.value)


def test_build_agent_fails_fast_without_pyinstaller(tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(build_mod, "BUILD_TMP", tmp_path / "build-tmp")
    monkeypatch.setattr(build_mod.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(BuildError) as excinfo:
        build_mod.build_agent(token=TOKEN_A)
    assert "requirements.txt" in str(excinfo.value)
    assert not (tmp_path / "build-tmp").exists()


def test_archive_stale_output(tmp_path):
    missing = tmp_path / "missing.exe"
    assert archive_stale_output(str(missing)) is None
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"x")
    backup = archive_stale_output(str(exe))
    assert backup is not None and backup != str(exe)
    assert not exe.exists() and Path(backup).exists()


def test_build_aborts_fast_on_bad_token_and_keeps_output(tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "DIST_DIR", tmp_path)
    monkeypatch.setattr(build_mod, "OUTPUT_EXE", tmp_path / "AstralRemote-Agent.exe")
    monkeypatch.setattr(build_mod, "BUILD_TMP", tmp_path / "build-tmp")
    before = None
    with pytest.raises(BuildError):
        build_mod.build_agent(token="junk")
    assert (tmp_path / "AstralRemote-Agent.exe").exists() is False


def test_signal_handler_kills_child_and_raises():
    import signal as signal_mod

    killed = []

    class FakeChild:
        def kill(self):
            killed.append(True)

    build_mod._current_child = FakeChild()
    try:
        with pytest.raises(KeyboardInterrupt):
            build_mod._on_signal(signal_mod.SIGINT, None)
    finally:
        build_mod._current_child = None
    assert killed == [True]


def test_generate_cancel_returns_to_menu(monkeypatch):
    import sys

    from rich.console import Console

    import astral_remote.cli.menu as menu_mod

    def cancelled(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(build_mod, "build_agent", cancelled)
    monkeypatch.setattr(build_mod, "prompt_build_token", lambda **kwargs: TOKEN_A)
    stdin = _FakePipe()
    monkeypatch.setattr(sys, "stdin", stdin)
    console = Console(file=_FakePipe(), force_terminal=False, no_color=True)
    assert menu_mod.run_generate(console, False) is False


def test_start_interrupt_returns_to_menu(monkeypatch):
    import sys

    import astral_remote.agent.app as app_mod
    import astral_remote.agent.secure_input as secure_mod
    import astral_remote.cli.menu as menu_mod
    from rich.console import Console

    monkeypatch.setattr(secure_mod, "prompt_for_token", lambda: TOKEN_A)

    async def cancelled(token=None, config=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr(app_mod, "run_agent", cancelled)
    stdin = _FakePipe()
    monkeypatch.setattr(sys, "stdin", stdin)
    console = Console(file=_FakePipe(), force_terminal=False, no_color=True)
    assert menu_mod.run_start(console, False) is None
