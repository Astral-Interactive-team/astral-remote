# Development CLI application: `python -m astral_remote`.
# Opens the interactive menu by default; `build` regenerates the EXE
# non-interactively through the same pipeline as menu option 01.
# Ported from bin/astral-remote.js.

from __future__ import annotations

import argparse
import base64
import sys

from astral_remote import AGENT_VERSION


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="astral-remote", description="Build and run AstralRemote Agent EXE.")
    parser.add_argument("--version", "-v", action="store_true", help="Print version and exit.")
    sub = parser.add_subparsers(dest="command")
    build_cmd = sub.add_parser("build", help="Generate dist/astral-remote.exe.")
    build_cmd.add_argument("--version", "-v", action="store_true", help="Print version and exit.")
    credentials_cmd = sub.add_parser("credentials", help="Manage local Host credentials.")
    credentials_cmd.add_argument("action", choices=("set", "clear"))
    identity_cmd = sub.add_parser("identity", help="Show local cryptographic node identity.")
    identity_cmd.add_argument("action", choices=("show",), nargs="?", default="show")
    return parser


def main(argv=None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"astral-remote {AGENT_VERSION}")
        return 0
    if getattr(args, "command", None) == "build":
        from rich.console import Console

        from astral_remote.cli.menu import run_generate
        from astral_remote.cli.theme import supports_color

        enabled = supports_color()
        console = Console(no_color=not enabled, highlight=False)
        return 0 if run_generate(console, enabled) else 1
    if getattr(args, "command", None) == "credentials":
        from astral_remote.agent.secure_input import prompt_for_token
        from astral_remote.agent.payload import is_plausible_bot_token
        from astral_remote.shared.security import HostCredentialStore, SecurityStoreError
        try:
            store = HostCredentialStore()
            if args.action == "clear":
                store.clear()
                print("Host credential removed.")
            else:
                token = prompt_for_token()
                if not is_plausible_bot_token(token):
                    raise SecurityStoreError("Host credential format is invalid.")
                store.save_token(token)
                print("Host credential saved.")
            return 0
        except (SecurityStoreError, OSError, ValueError) as exc:
            print(f"Credential operation failed: {exc}", file=sys.stderr)
            return 1
    if getattr(args, "command", None) == "identity":
        from astral_remote.shared.security import IdentityStore, SecurityStoreError
        try:
            identity = IdentityStore().load_or_create()
            print(f"Node ID: {identity.node_id}")
            print(f"Algorithm: {identity.algorithm}")
            print("Public key: " + base64.b64encode(identity.public_key).decode("ascii"))
            return 0
        except SecurityStoreError as exc:
            print(f"Identity operation failed: {exc}", file=sys.stderr)
            return 1
    from astral_remote.cli.menu import run_menu

    try:
        return run_menu()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard, message is sanitized
        print(f"Menu failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
