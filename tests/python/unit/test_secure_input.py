# Unit tests for secure input. Mirrors tests/secure-input.test.js guarantees:
# no token in output, empty rejected, non-TTY refuses instead of echoing.

import io

import pytest

from astral_remote.agent.secure_input import (
    INTERRUPTED,
    InterruptedError,
    prompt_for_token,
    read_secure_line,
    void_header,
    void_prompt,
)


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class FakePipe(io.StringIO):
    def isatty(self):
        return False


def test_non_tty_refuses():
    with pytest.raises(OSError):
        read_secure_line(input_stream=FakePipe(), output_stream=FakePipe())


def test_prompt_rejects_empty_with_fake_msvcrt(monkeypatch):
    import astral_remote.agent.secure_input as mod

    out = FakeTTY()
    monkeypatch.setattr(mod.sys, "stdin", FakeTTY())
    monkeypatch.setattr(mod, "read_secure_line", lambda prefix="", input_stream=None, output_stream=None: "   ")
    with pytest.raises(ValueError, match="Empty token"):
        prompt_for_token(output_stream=out)
    assert void_header() in out.getvalue()
    assert void_prompt("[Discord Bot Token]") in out.getvalue()


def test_prompt_without_header_keeps_single_block(monkeypatch):
    import astral_remote.agent.secure_input as mod

    out = FakeTTY()
    monkeypatch.setattr(mod, "read_secure_line", lambda prefix="", input_stream=None, output_stream=None: "tok123")
    assert prompt_for_token(output_stream=out, show_header=False) == "tok123"
    written = out.getvalue()
    assert void_header() not in written
    assert void_prompt("[Discord Bot Token]") not in written
    assert "[Discord Bot Token]: " in written


def test_prompt_returns_trimmed_without_printing(monkeypatch):
    import astral_remote.agent.secure_input as mod

    out = FakeTTY()
    monkeypatch.setattr(mod, "read_secure_line", lambda prefix="", input_stream=None, output_stream=None: "  tok123  ")
    assert prompt_for_token(output_stream=out) == "tok123"
    assert "tok123" not in out.getvalue()


def test_prompt_q_cancels_like_quit(monkeypatch):
    import astral_remote.agent.secure_input as mod

    out = FakeTTY()
    monkeypatch.setattr(mod, "read_secure_line", lambda prefix="", input_stream=None, output_stream=None: "q")
    with pytest.raises(InterruptedError):
        prompt_for_token(output_stream=out)
    monkeypatch.setattr(mod, "read_secure_line", lambda prefix="", input_stream=None, output_stream=None: "  Q  ")
    with pytest.raises(InterruptedError):
        prompt_for_token(output_stream=out)


def test_start_cancels_back_to_menu(monkeypatch):
    import astral_remote.agent.secure_input as secure_mod
    from astral_remote.cli.menu import run_start
    from rich.console import Console

    def cancelled():
        raise InterruptedError(INTERRUPTED)

    monkeypatch.setattr(secure_mod, "prompt_for_token", cancelled)
    console = Console(file=FakeTTY(), force_terminal=False, no_color=True)
    assert run_start(console, False) is None


def test_interrupted_constant():
    assert INTERRUPTED == "INTERRUPTED"
    assert issubclass(InterruptedError, Exception)


def test_masked_entry_value_and_pending_drain(monkeypatch):
    import sys as sys_mod

    import astral_remote.agent.secure_input as mod

    keys = ["a", "b", "\r", "\n", "z"]

    class FakeMsvcrt:
        @staticmethod
        def getwch():
            return keys.pop(0)

        @staticmethod
        def kbhit():
            return len(keys) > 0

    monkeypatch.setitem(sys_mod.modules, "msvcrt", FakeMsvcrt)
    out = FakeTTY()
    assert mod.read_secure_line(input_stream=FakeTTY(), output_stream=out) == "ab"
    assert out.getvalue().count("*") >= 2
    assert keys == []  # leftover newline and typeahead drained
    assert "ab" not in out.getvalue()
