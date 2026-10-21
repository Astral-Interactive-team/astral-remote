# Secure terminal input for bot token entry.
# Masks typing with * on a Windows TTY via msvcrt, suspends Rich Live
# rendering while reading so the display cannot be corrupted. After entry
# the line is rewritten with stars only. Token value never printed.
# Ctrl+C aborts with InterruptedError. Non-TTY fails clearly instead of
# echoing the token. Ported from src/agent/secure-input.js.

from __future__ import annotations

import sys

INTERRUPTED = "INTERRUPTED"


class InterruptedError(Exception):
    """Raised when token entry is cancelled with Ctrl+C."""


def void_header() -> str:
    """Void-style prompt header shared by dev menu and build prompt."""
    return "┌─[void@tool]──[~/home]"


def void_prompt(label: str) -> str:
    """Void-style prompt prefix for input lines."""
    return f"└── >> {label}: "


def _is_tty(stream) -> bool:
    try:
        return stream is not None and stream.isatty()
    except (OSError, ValueError):
        return False


def _drain_pending_input() -> None:
    """Discard leftover keystrokes (e.g. the Enter newline) from the console queue.

    msvcrt.getwch consumes Enter's \\r but leaves \\n behind; the next
    buffered stdin read would then return instantly instead of waiting.
    Typeahead is intentionally discarded so the following prompt starts
    clean. Non-blocking; never raises.
    """
    try:
        import msvcrt
    except ImportError:
        return
    try:
        while msvcrt.kbhit():
            msvcrt.getwch()
    except (OSError, ValueError):
        pass


def read_secure_line(prefix: str = "", input_stream=None, output_stream=None) -> str:
    """Read one line with per-character masking. Returns untrimmed text."""
    inp = input_stream if input_stream is not None else sys.stdin
    out = output_stream if output_stream is not None else sys.stderr
    if not _is_tty(inp) or not _is_tty(out):
        raise OSError("Secure input requires an interactive terminal; refusing to echo a token.")
    try:
        import msvcrt
    except ImportError:
        raise OSError("Secure input requires Windows console support; refusing to echo a token.")
    buffer: list[str] = []

    def write(text: str) -> None:
        try:
            out.write(text)
            out.flush()
        except (OSError, ValueError):
            pass

    try:
        while True:
            try:
                ch = msvcrt.getwch()
            except (OSError, ValueError) as exc:
                raise OSError(f"Secure input failed: {exc}") from exc
            if ch in ("\r", "\n"):
                write("\r\n")
                _drain_pending_input()
                break
            if ch == "\x03":
                write("\n")
                _drain_pending_input()
                raise InterruptedError(INTERRUPTED)
            if ch in ("\x08", "\x7f"):
                if buffer:
                    buffer.pop()
                    write("\b \b")
                continue
            if ch >= " " and ch != "\x7f":
                buffer.append(ch)
                write("*")
    except InterruptedError:
        raise
    return "".join(buffer)


def _styled_line(text: str, enabled: bool) -> str:
    """Render text with the purple theme as an ANSI string for raw stream writes.

    Reuses Rich styling so colors match the menu exactly. Returns plain
    text when disabled. ANSI codes are zero-width, so line alignment and
    the star-conceal rewrite are unaffected.
    """
    import io

    from rich.console import Console as RichConsole

    from astral_remote.cli.theme import style_text

    buffer = io.StringIO()
    console = RichConsole(file=buffer, force_terminal=True, no_color=not enabled, highlight=False)
    console.print(style_text(text, enabled), end="")
    return buffer.getvalue()


def prompt_for_token(output_stream=None, show_header: bool = True) -> str:
    """Prompt for the bot token with void header. Trims, rejects empty. Never prints value.

    `show_header` prints the void-style header plus `└── >> ` prefix.
    Callers that already keep the menu prompt on screen (the interactive
    generate step) pass False so only a single prompt block is visible;
    the token line then mirrors the plain exe-name prompt.
    """
    from astral_remote.cli.theme import supports_color

    out = output_stream if output_stream is not None else sys.stderr
    enabled = supports_color()
    if show_header:
        prefix = void_prompt("[Discord Bot Token]")
        prompt_text = _styled_line(void_header() + "\n", enabled) + _styled_line(prefix, enabled)
    else:
        prefix = "[Discord Bot Token]: "
        prompt_text = _styled_line(prefix, enabled)
    try:
        out.write(prompt_text)
        out.flush()
    except (OSError, ValueError):
        pass
    token = read_secure_line(prefix=prefix, output_stream=out).strip()
    if token == "":
        raise ValueError("Empty token rejected. Paste a bot token from the Discord developer portal.")
    if token.lower() == "q":
        # Bare q cancels entry like menu quit. A real token is three
        # dot-separated parts, so this can never be a valid token.
        raise InterruptedError(INTERRUPTED)
    return token
