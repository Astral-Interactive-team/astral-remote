# Unit tests for Rich views: banner, Kali menu layout, prompt, NO_COLOR.
# Layout mirrors src/cli/banner.js blocks exactly; Rich provides styling
# only, never Panel borders around banner or menu.

from rich.console import Console

from astral_remote.cli import views
from astral_remote.cli.links import SOCIAL_LINKS
from astral_remote.cli.theme import DARK_PURPLE, PURPLE, style_text, supports_color

LEFT_CELL = " │   ├── [01] GitHub"
RIGHT_CELL = "│  [02] Telegram"
EXPECTED_HOME = [
    " ┌── HOME · 1/2",
    " │",
    " ├── ASTRAL INTERACTIVE",
    LEFT_CELL + " " * (77 - len(LEFT_CELL)) + RIGHT_CELL,
    " │   └── [03] Discord",
    " │",
    " ├── [N] next ──> REMOTE",
    " │",
    " └── [Q] quit",
    "",
]

EXPECTED_HOME_NARROW = [
    " ┌── HOME · 1/2",
    " │",
    " ├── ASTRAL INTERACTIVE",
    " │   ├── [01] GitHub",
    " │   ├── [02] Telegram",
    " │   └── [03] Discord",
    " │",
    " ├── [N] next ──> REMOTE",
    " │",
    " └── [Q] quit",
    "",
]

EXPECTED_REMOTE = [
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
]


def _capture(renderable, width=120, no_color=True, force_terminal=False):
    console = Console(width=width, no_color=no_color, highlight=False,
                      force_terminal=force_terminal)
    with console.capture() as capture:
        views.print_block(console, renderable)
    return capture.get()


def test_home_layout_exact():
    assert views.home_block(False).plain.split("\n") == EXPECTED_HOME


def test_home_narrow_layout_stacked():
    assert views.home_block(False, width=40).plain.split("\n") == EXPECTED_HOME_NARROW


def test_home_names_only_urls_stay_in_config():
    wide = views.home_block(False).plain
    assert "https://" not in wide
    assert SOCIAL_LINKS["github"].endswith("/astral-remote")
    assert SOCIAL_LINKS["telegram"] == "https://t.me/astral_int"
    assert SOCIAL_LINKS["discord"] == "https://discord.gg/bSvcQG8K4f"


def test_remote_layout_exact():
    assert views.remote_block(False).plain.split("\n") == EXPECTED_REMOTE


def test_no_panel_borders():
    for block in (views.home_block(False), views.remote_block(False),
                  views.home_block(True), views.remote_block(True)):
        assert "╭" not in block.plain and "╰" not in block.plain
        assert "╮" not in block.plain and "╯" not in block.plain


def test_prompt_exact_path():
    line1, line2 = views.kali_prompt_text(False)
    assert line1.plain == "┌─[astral@remote]──[~/home]"
    assert line2.plain == "└── >> "
    assert "Astral_int" not in line1.plain


def test_prompt_colored_contains_same_text():
    line1, line2 = views.kali_prompt_text(True)
    assert line1.plain == "┌─[astral@remote]──[~/home]"
    assert line2.plain == "└── >> "


def test_load_banner_matches_file_bytes():
    raw = views.load_banner_text()
    assert raw == views.load_banner_text(views.BANNER_PATH)
    assert "discord.gg/bSvcQG8K4f" in raw
    assert views.BANNER_PATH.name == "banner.txt"
    assert views.BANNER_PATH.parent.name == "cli"


def test_banner_full_url_preserved_narrow():
    raw = views.load_banner_text()
    out = _capture(views.render_banner_text(raw, False), width=40)
    assert "discord.gg/bSvcQG8K4f" in out
    assert "t.me/astral_int" in out


def test_menu_urls_preserved_narrow():
    out = _capture(views.home_block(False, width=40), width=40)
    assert "GitHub" in out and "Telegram" in out and "Discord" in out
    assert "https://" not in out


def test_center_text_pads_and_passes_through():
    assert views.center_text("ab", 10) == "    ab"
    assert views.center_text("abcdefghij", 8) == "abcdefghij"
    assert views.center_text("ab", None) == "ab"


def test_supports_color_gating():
    assert supports_color({"NO_COLOR": ""}, type("S", (), {"isatty": lambda s: True})()) is False
    assert supports_color({}, type("S", (), {"isatty": lambda s: False})()) is False
    assert supports_color({}, type("S", (), {"isatty": lambda s: True})()) is True


def test_colored_output_has_ansi_plain_does_not():
    colored = _capture(views.home_block(True), no_color=False, force_terminal=True)
    plain = _capture(views.home_block(False), no_color=True)
    assert "\x1b[" in colored
    assert "\x1b[" not in plain
    assert "GitHub" in plain and "[01]" in plain


def _span_styles(text):
    return [(text.plain[s:e], str(style)) for s, e, style in _spans(text)]


def _spans(text):
    for span in text.spans:
        yield span.start, span.end, span.style


def test_style_categories():
    styled = style_text("A┌ 가019", True)
    assert styled.plain == "A┌ 가019"
    by_text = {chunk: style for chunk, style in _span_styles(styled)}
    assert by_text.get("A") == PURPLE
    assert by_text.get("019") == PURPLE
    assert by_text.get("┌") == DARK_PURPLE
    assert " " not in by_text and "가" not in by_text


def test_style_disabled_and_empty():
    assert style_text("A┌", False).plain == "A┌"
    assert style_text("A┌", False).spans == []
    assert style_text("", True).plain == ""


def test_menu_spans_use_theme_colors():
    block = views.home_block(True)
    styles = {str(style) for _, _, style in _spans(block)}
    assert PURPLE in styles
    assert DARK_PURPLE in styles
    assert block.plain.split("\n")[0] == " ┌── HOME · 1/2"


def test_banner_styled_preserves_content():
    raw = views.load_banner_text()
    styled = views.render_banner_text(raw, True)
    assert styled.plain == views.center_text(raw, None)
    assert "discord.gg/bSvcQG8K4f" in styled.plain


def test_prompt_spans():
    line1, line2 = views.kali_prompt_text(True)
    styles = {str(style) for _, _, style in _spans(line1)}
    assert PURPLE in styles and DARK_PURPLE in styles
    assert line1.plain == "┌─[astral@remote]──[~/home]"
    assert line2.plain == "└── >> "
