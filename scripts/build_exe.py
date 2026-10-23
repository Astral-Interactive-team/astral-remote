# Production EXE build script: python scripts/build_exe.py
# Prompts for the token with masked input, the output name, the bot
# command prefix, and the self-install choices, then runs the isolated
# staging PyInstaller onefile build. Run from the repository root.

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astral_remote.builder.build import (  # noqa: E402
    build_agent,
    prompt_autostart,
    prompt_exe_name,
    prompt_install_appdata,
    prompt_prefix,
)


def main() -> int:
    try:
        result = build_agent(
            output_name=prompt_exe_name(),
            prefix=prompt_prefix(),
            install_to_appdata=prompt_install_appdata(),
            enable_autostart=prompt_autostart(),
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - build errors are user-safe by construction
        from astral_remote.agent.secure_input import InterruptedError as TokenCancelled

        if isinstance(exc, TokenCancelled):
            print("Cancelled.")
            return 1
        print(f"Build failed: {exc}")
        return 1
    print(f"Build passed: {result['exe_path']}")
    print(f"Command prefix: {result['prefix']}")
    print(f"Smoke test: {result['version_line']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
