# Astral Interactive terminal theme for Rich output.
# Single place for color decisions. Colors stay off when NO_COLOR is set
# or stdout is not a TTY, so redirected output and tests stay plain.
# Ported from src/cli/theme.js; Rich styles replace raw ANSI codes.

from __future__ import annotations

import os
import sys

CYAN = "cyan"
PURPLE = "#A855F7"
DARK_PURPLE = "#5B21B6"
GREEN = "green"
WHITE = "white"
GRAY = "grey62"
RED = "red"
YELLOW = "yellow"

GRADIENT_FROM = (0, 255, 255)
GRADIENT_TO = (168, 85, 247)

_STYLE_PURPLE = PURPLE
_STYLE_DARK = DARK_PURPLE

_WHITESPACE = (" ", "\t", "\n", "\r")


def _char_category(ch: str):
    """Classify one character: 'purple', 'dark', or None (unchanged).

    English letters and digits take purple. Punctuation, ASCII symbols,
    box-drawing characters, arrows, and decorative symbols take dark
    purple. Whitespace, Korean text, and other letters stay unchanged so
    layout and spacing remain stable.
    """
    if ch in _WHITESPACE:
        return None
    if "A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9":
        return "purple"
    code = ord(ch)
    if code < 128:
        return "dark"
    if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return None
    import unicodedata

    if unicodedata.category(ch).startswith("L"):
        return None
    return "dark"


def style_text(text: str, enabled: bool):
    """Apply per-character purple styling, preserving every character.

    Adjacent characters with the same category merge into single spans
    for efficient rendering. Returns plain text when disabled. Stripping
    ANSI styling always reproduces the exact original string.
    """
    from rich.text import Text

    out = Text(no_wrap=True)
    if not enabled or not text:
        out.append(text)
        return out
    current = None
    run_chars: list[str] = []
    for ch in text:
        category = _char_category(ch)
        if category != current:
            if run_chars:
                style = "none" if current is None else (_STYLE_PURPLE if current == "purple" else _STYLE_DARK)
                out.append("".join(run_chars), style=style)
                run_chars = []
            current = category
        run_chars.append(ch)
    if run_chars:
        style = "none" if current is None else (_STYLE_PURPLE if current == "purple" else _STYLE_DARK)
        out.append("".join(run_chars), style=style)
    return out


def supports_color(env=None, stream=None) -> bool:
    """Return True when colored output is wanted.

    NO_COLOR disables in all cases; otherwise a TTY stream is required.
    """
    source = env if env is not None else os.environ
    if source is not None and "NO_COLOR" in source:
        return False
    target = stream if stream is not None else sys.stdout
    try:
        return target is not None and target.isatty() is True
    except (OSError, ValueError):
        return False


def mix_rgb(frm: tuple, to: tuple, factor: float) -> tuple:
    """Interpolate an RGB pair by factor 0..1. Used for the banner gradient."""
    return tuple(round(f + (t - f) * factor) for f, t in zip(frm, to))
