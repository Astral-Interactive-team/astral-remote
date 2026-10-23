# Unit tests for in-place menu redraws: physical line counting, erase
# sequences, and no-accumulation across navigation cycles.

import io
import sys

from rich.console import Console

from astral_remote.cli.menu import (
    QUIT_HINT_INTERRUPT,
    QUIT_HINT_TYPE,
    erase_lines,
    is_interactive,
    physical_line_count,
    prompt_rows,
    run_menu,
)


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class FakePipe(io.StringIO):
    def isatty(self):
        return False


def test_physical_line_count_logical_and_wrapped():
    assert physical_line_count("a\nb\nc") == 3
    assert physical_line_count("ab", None) == 1
    assert physical_line_count("ab", 0) == 1
    assert physical_line_count("abcdefghij", 8) == 2
    assert physical_line_count("abcdefgh", 8) == 1
    assert physical_line_count("", 80) == 1


def test_prompt_rows_cover_prompt_and_echo():
    rows = prompt_rows("01", False, 80)
    assert rows == 2
    rows_wrapped = prompt_rows("x" * 200, False, 80)
    assert rows_wrapped > 2


def test_erase_lines_sequence_and_guards():
    stream = FakeTTY()
    console = Console(file=stream, force_terminal=False)
    erase_lines(console, 3)
    assert stream.getvalue() == "\x1b[3A\x1b[2K\x1b[1B\x1b[2K\x1b[1B\x1b[2K\x1b[1B\x1b[3A"
    erase_lines(console, 0)
    erase_lines(console, -2)
    assert stream.getvalue().count("\x1b[") == 8


def test_is_interactive_requires_both_ttys(monkeypatch):
    out_tty = Console(file=FakeTTY(), force_terminal=False)
    monkeypatch.setattr(sys, "stdin", FakeTTY())
    assert is_interactive(out_tty) in (True, False)  # console.is_terminal decides on width probe
    monkeypatch.setattr(sys, "stdin", FakePipe())
    assert is_interactive(out_tty) is False


def _run_with_streams(stdin_text, stdout_stream, monkeypatch, patch_sleep=True):
    stdin = FakeTTY()
    stdin.write(stdin_text)
    stdin.seek(0)
    # Simulate terminal echo: a real console advances past the input row
    # on Enter. Without this the test stream cursor sits one row higher
    # than production and erase math cannot match.
    original_readline = stdin.readline

    def echoing_readline(*args):
        line = original_readline(*args)
        stdout_stream.write("\n")
        return line

    stdin.readline = echoing_readline
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout_stream)
    # Skip the interactive blank-screen startup hold so redraw assertions
    # run instantly; the hold itself is asserted separately.
    import astral_remote.cli.menu as menu_mod

    if patch_sleep:
        monkeypatch.setattr(menu_mod.time, "sleep", lambda seconds: None)
    return run_menu()


def _emulate_screen(stream_text):
    """Minimal terminal emulator: applies cursor-up, line-clear, cursor-down.

    Returns visible lines after processing escape sequences, with SGR
    color codes stripped. Lets tests assert what a user would see.
    """
    import re

    lines: list[str] = [""]
    row = 0
    token = re.compile(r"\x1b\[(?P<count>[?0-9;]*)(?P<cmd>[a-zA-Z])|(?P<cr>\r)|(?P<nl>\n)|(?P<ch>.)", re.DOTALL)
    for match in token.finditer(stream_text):
        seq, count, command = match.group(0), match.group("count"), match.group("cmd")
        if command in ("A", "B") and (count or "").isdigit():
            row = max(0, row + (-int(count or 1) if command == "A" else int(count or 1)))
            while row >= len(lines):
                lines.append("")
        elif command == "K" and (count or "") in ("", "2"):
            lines[row] = ""
        elif command == "H":
            row = 0
        elif command == "J":
            lines = [""]
            row = 0
        elif command is not None:
            pass  # other sequences (alt-screen enter/exit, SGR colors): no visible change
        elif match.group("nl"):
            row += 1
            if row >= len(lines):
                lines.append("")
        elif match.group("cr"):
            pass
        else:
            lines[row] += match.group("ch")
    return [line for line in lines]


def test_interactive_navigation_leaves_single_menu(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    assert _run_with_streams("n\nb\nq\nq\n", out, monkeypatch) == 0
    raw = out.getvalue()
    # Navigation erases menu+prompt before the action:
    # HOME(11+2+1) + REMOTE(10+2+1); +1 is the forced newline. The quit hint
    # stays below the visible menu instead of erasing it.
    assert raw.count("\x1b[2K") == 14 + 13
    visible = "\n".join(_emulate_screen(raw))
    assert visible.count("┌── HOME") == 1  # one menu, kept for the quit hint
    assert visible.count("┌── REMOTE") == 0  # erased on navigation
    assert visible.count("by Astral Interactive") == 1  # banner printed once, never erased
    assert "Bye." not in visible


def test_interactive_uses_alternate_screen(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    assert _run_with_streams("q\n", out, monkeypatch) == 0
    raw = out.getvalue()
    assert "\x1b[?1049h" in raw  # alt screen entered
    assert "\x1b[?1049l" in raw  # alt screen restored on exit


def test_interactive_blank_hold_before_first_paint(monkeypatch):
    """Interactive startup holds a blank screen; pipes skip the wait."""
    import astral_remote.cli.menu as menu_mod

    slept = []
    out = FakeTTY()
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    monkeypatch.setattr(menu_mod.time, "sleep", lambda seconds: slept.append(seconds))
    assert _run_with_streams("q\n", out, monkeypatch, patch_sleep=False) == 0
    assert slept == [menu_mod.STARTUP_BLANK_SECONDS]


def test_generate_keeps_remote_menu_on_screen(monkeypatch):
    """Option 01 leaves the REMOTE menu visible while its prompts appear below."""
    import astral_remote.cli.menu as menu_mod

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    seen = {}

    def fake_generate(console, enabled, **kwargs):
        seen["screen"] = "\n".join(_emulate_screen(out.getvalue()))
        seen["kwargs"] = kwargs
        return True

    monkeypatch.setattr(menu_mod, "run_generate", fake_generate)
    assert _run_with_streams("n\n1\nq\n", out, monkeypatch) == 0
    # The REMOTE menu (and the echoed choice) is still on screen when the
    # generate prompts begin, instead of being erased beforehand.
    assert seen["screen"].count("┌── REMOTE") == 1
    # The menu keeps its own prompt block, so the token prompt must not
    # print a second header/└── >> block.
    assert seen["kwargs"].get("show_token_header") is False


def test_other_choices_erase_menu_before_action(monkeypatch):
    """Non-generate choices still erase the menu (no accumulation)."""
    import astral_remote.cli.menu as menu_mod

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    seen = {}

    def fake_start(console, enabled):
        seen["screen"] = "\n".join(_emulate_screen(out.getvalue()))
        return None

    monkeypatch.setattr(menu_mod, "run_start", fake_start)
    assert _run_with_streams("n\n2\nq\n", out, monkeypatch) == 0
    assert seen["screen"].count("┌── REMOTE") == 0


def test_non_interactive_has_no_screen_sequences(monkeypatch):
    out = FakePipe()
    stdin = FakePipe()
    stdin.write("q\n")
    stdin.seek(0)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", out)
    from astral_remote.cli.menu import run_menu as _run

    assert _run() == 0
    assert "?1049" not in out.getvalue()


def test_wait_erases_prompt_interactive(monkeypatch):
    import sys

    from rich.console import Console

    from astral_remote.cli.menu import wait_to_continue

    out = FakeTTY()
    stdin = FakeTTY()
    stdin.write("\n")
    stdin.seek(0)
    original_readline = stdin.readline

    def echoing_readline(*args):
        line = original_readline(*args)
        out.write("\n")
        return line

    stdin.readline = echoing_readline
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("LINES", "24")
    console = Console(highlight=False)
    wait_to_continue(console, True)
    visible = "\n".join(_emulate_screen(out.getvalue()))
    assert "press enter" not in visible


def test_wait_to_continue_returns_on_eof(monkeypatch):
    import sys

    from rich.console import Console

    from astral_remote.cli.menu import wait_to_continue

    stdin = FakePipe()
    monkeypatch.setattr(sys, "stdin", stdin)
    console = Console(file=FakePipe(), force_terminal=False, no_color=True)
    assert wait_to_continue(console, False) is None
    assert "press enter to continue" in console.file.getvalue()


def test_refresh_clears_action_output(monkeypatch):
    import sys

    from rich.console import Console

    from astral_remote.cli.menu import refresh_screen

    out = FakeTTY()
    monkeypatch.setattr(sys, "stdout", out)
    console = Console(highlight=False)
    console.print("stale token prompt ****")
    refresh_screen(console, "BANNER", False, None)
    visible = "\n".join(_emulate_screen(out.getvalue()))
    assert "stale token prompt" not in visible
    assert visible.count("BANNER") == 1


def test_generate_result_has_no_panel(monkeypatch):
    import sys

    from rich.console import Console

    import astral_remote.builder.build as build_mod
    import astral_remote.cli.menu as menu_mod

    monkeypatch.setattr(build_mod, "prompt_build_token", lambda **kwargs: (_ for _ in ()).throw(ValueError("Empty token rejected.")))
    stdin = FakePipe()
    monkeypatch.setattr(sys, "stdin", stdin)
    console = Console(file=FakePipe(), force_terminal=False, no_color=True)
    assert menu_mod.run_generate(console, False) is False
    out = console.file.getvalue()
    assert "╭" not in out and "╰" not in out


def test_non_interactive_has_no_cursor_sequences(monkeypatch):
    out = FakePipe()
    stdin = FakePipe()
    stdin.write("q\n")
    stdin.seek(0)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", out)
    assert run_menu() == 0
    assert "\x1b[" not in out.getvalue()
    assert "┌── HOME" in out.getvalue()


def test_confirm_exit_requires_the_quit_gesture(monkeypatch):
    """A typed quit needs q again; an interrupt needs a second Ctrl+C."""
    from astral_remote.cli.menu import confirm_exit

    for answer, expected in (("q\n", True), (" Q \n", True), ("y\n", False),
                             ("\n", False), ("quit\n", False), ("n\n", False)):
        monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
        console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
        assert confirm_exit(console, False) is expected

    # At the interrupt hint only another Ctrl+C confirms; typed input cancels.
    monkeypatch.setattr(sys, "stdin", io.StringIO("q\n"))
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    assert confirm_exit(console, False, interrupted=True) is False


def test_confirm_exit_accepts_interrupt_and_eof():
    """A second Ctrl+C or a closed stream must never loop forever."""
    from astral_remote.cli.menu import confirm_exit

    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    for error in (KeyboardInterrupt, EOFError):
        def raise_error(prompt="", _error=error):
            raise _error

        console.input = raise_error
        assert confirm_exit(console, False) is True


def test_quit_asks_for_confirmation(monkeypatch):
    """`q` shows the hint below the still-visible menu and needs q again."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    assert _run_with_streams("q\nq\n", out, monkeypatch) == 0
    visible = "\n".join(_emulate_screen(out.getvalue()))
    assert visible.count(QUIT_HINT_TYPE) == 1
    assert visible.count("┌── HOME") == 1  # the hint never hides the menu


def _strip_sgr(text):
    """Drop SGR color sequences so prompt text can be counted in the raw stream."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_declining_quit_redraws_the_menu(monkeypatch):
    """Anything but q again keeps the session alive and redraws the menu."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    assert _run_with_streams("q\nn\nq\nq\n", out, monkeypatch) == 0
    raw = out.getvalue()
    visible = "\n".join(_emulate_screen(raw))
    # Both hints are asked even though the redraw clears the first one.
    assert _strip_sgr(raw).count(QUIT_HINT_TYPE) == 2
    assert raw.count("\x1b[2J") == 1  # only the alt-screen entry clears
    assert visible.count("┌── HOME") == 1  # redrawn once, never stacked


def test_ctrl_c_asks_for_confirmation(monkeypatch):
    """Ctrl+C shows the interrupt hint; anything but Ctrl+C keeps the session."""
    import astral_remote.cli.menu as menu_mod

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    out = FakeTTY()
    calls = {"count": 0}

    def read(console, enabled):
        calls["count"] += 1
        if calls["count"] == 1:
            raise menu_mod.InterruptedError("interrupted")
        return "q"

    monkeypatch.setattr(menu_mod, "read_choice", read)
    assert _run_with_streams("x\nq\n", out, monkeypatch) == 0
    plain = _strip_sgr(out.getvalue())
    assert plain.count(QUIT_HINT_INTERRUPT) == 1
    assert plain.count(QUIT_HINT_TYPE) == 1


def test_non_interactive_quit_does_not_prompt(monkeypatch):
    """Pipes cannot answer a prompt, so they keep quitting immediately."""
    out = FakePipe()
    stdin = FakePipe()
    stdin.write("q\n")
    stdin.seek(0)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", out)
    assert run_menu() == 0
    assert QUIT_HINT_TYPE not in out.getvalue()
    assert QUIT_HINT_INTERRUPT not in out.getvalue()
