# Rich views for the interactive CLI: banner, menus, prompts, status.
# Kali-style plain layout matching src/cli/banner.js exactly. Colors
# follow the two-purple theme (dark purple specials, purple English and
# digits, Korean and whitespace unchanged) applied character by
# character through theme.style_text. No Panel wraps the banner or menu
# blocks. Long lines are never clipped: blocks use no_wrap and print
# with overflow ignored.

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from astral_remote.cli.theme import (
    CYAN,
    GREEN,
    YELLOW,
    style_text,
)

BANNER_PATH = Path(__file__).resolve().parent / "banner.txt"


def load_banner_text(path=None) -> str:
    """Load raw banner text. Returns '' when missing so menu still starts."""
    try:
        return Path(path if path is not None else BANNER_PATH).read_text(encoding="utf-8")
    except OSError:
        return ""


def center_text(text: str, width=None) -> str:
    """Center each line in the given width by left padding. Null width passthrough."""
    if not isinstance(width, int) or width <= 0 or text == "":
        return text
    trailing = "\n" if text.endswith("\n") else ""
    body = text[:-1] if trailing else text
    centered = []
    for line in body.split("\n"):
        pad = (width - len(line)) // 2
        centered.append((" " * pad + line) if pad > 0 else line)
    return "\n".join(centered) + trailing


def render_banner_text(text: str, enabled: bool, width=None) -> Text:
    """Style banner art with the purple theme. Characters and breaks preserved."""
    return style_text(center_text(text, width), enabled)


def print_block(console: Console, renderable) -> None:
    """Print a banner or menu block without wrapping or clipping long lines."""
    console.print(renderable, soft_wrap=True, overflow="ignore")


# Second column of the HOME two-column row starts at this plain-text
# column. Rows below this width use a stacked single-column fallback so
# narrow terminals never clip content.
_HOME_SECOND_COLUMN = 77
_HOME_TWO_COLUMN_MIN_WIDTH = 100


def _join_block(lines: list[str], enabled: bool) -> Text:
    """Style plain layout lines and join them into one block."""
    block = Text(no_wrap=True)
    for index, line in enumerate(lines):
        block.append_text(style_text(line, enabled))
        if index < len(lines) - 1:
            block.append("\n")
    return block


def _home_option_lines() -> list[str]:
    """HOME option rows with GitHub + Telegram side by side, Discord below."""
    left = " │   ├── [01] GitHub"
    padding = " " * max(0, _HOME_SECOND_COLUMN - len(left))
    return [
        left + padding + "│  [02] Telegram",
        " │   └── [03] Discord",
    ]


def _home_option_lines_stacked() -> list[str]:
    """HOME option rows stacked one per line for narrow terminals."""
    return [
        " │   ├── [01] GitHub",
        " │   ├── [02] Telegram",
        " │   └── [03] Discord",
    ]


def home_block(enabled: bool, width=None) -> Text:
    """Build the HOME page block (1/2): social links menu. Plain Kali layout.

    Wide terminals show GitHub and Telegram on one row with Discord
    below; narrow terminals fall back to one option per row so nothing
    clips. Labels show service names only; URLs stay in configuration
    and open on selection.
    """
    lines = [
        " ┌── HOME · 1/2",
        " │",
        " ├── ASTRAL INTERACTIVE",
    ]
    if isinstance(width, int) and width < _HOME_TWO_COLUMN_MIN_WIDTH:
        lines.extend(_home_option_lines_stacked())
    else:
        lines.extend(_home_option_lines())
    lines.extend([
        " │",
        " ├── [N] next ──> REMOTE",
        " │",
        " └── [Q] quit",
        "",
    ])
    return _join_block(lines, enabled)


def remote_block(enabled: bool) -> Text:
    """Build the REMOTE page block (2/2): agent operations menu. Plain Kali layout."""
    return _join_block([
        " ┌── REMOTE · 2/2",
        " │",
        " ├── ASTRAL REMOTE",
        " │   ├── [01] Generate Agent EXE",
        " │   └── [02] Start Agent",
        " │",
        " ├── [B] back ──> HOME",
        " │",
        " └── [Q] quit",
        "",
    ], enabled)


def kali_prompt_text(enabled: bool) -> tuple[Text, Text]:
    """Kali-style two-line prompt: ┌─[astral@remote]──[~/home] / └── >> .

    Fixed path ~/home on both pages; never the working directory.
    Returns (line1, line2-with-trailing-space).
    """
    return style_text("┌─[astral@remote]──[~/home]", enabled), style_text("└── >> ", enabled)


def status_panel(kind: str, message: str, enabled: bool) -> Panel:
    """Colored status panel for transient messages. kind is ok, warn, error, or info."""
    styles = {"ok": GREEN, "warn": YELLOW, "error": "red", "info": CYAN}
    titles = {"ok": "OK", "warn": "Warning", "error": "Error", "info": "Info"}
    return Panel(style_text(message, enabled), title=titles.get(kind, "Info"),
                 border_style=styles.get(kind, CYAN) if enabled else "none")


__all__ = [
    "BANNER_PATH", "center_text", "home_block", "kali_prompt_text",
    "load_banner_text", "print_block", "remote_block", "render_banner_text",
    "status_panel",
]
