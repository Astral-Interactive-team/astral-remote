# Embedded payload code generation for single-file distribution.
# The builder writes `_embedded_payload.py` into an isolated staging
# package copy only; the real source tree never holds generated secret
# material. PyInstaller bundles the staged module as code, so no
# standalone plaintext token file is extracted at runtime beyond normal
# bundle contents. The token remains extractable from the executable;
# this module provides no encryption theater.

from __future__ import annotations

from astral_remote.agent.payload import PAYLOAD_VERSION, is_plausible_bot_token
from astral_remote.shared.config import validate_config
from astral_remote.shared.errors import BuildError

MAX_PAYLOAD_BYTES = 65536


def render_payload_module(token: str, config: dict) -> str:
    """Render the _embedded_payload.py source. Throws on bad token or config."""
    if not is_plausible_bot_token(token):
        raise BuildError("Token rejected: expected bot token shape (three dot-separated parts).")
    problems = validate_config(config)
    if problems:
        raise BuildError(f"Payload config invalid: {problems[0]}")
    body = f"PAYLOAD_V = {PAYLOAD_VERSION}\nTOKEN = {token.strip()!r}\nCONFIG = {config!r}\n"
    if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise BuildError("Payload too large.")
    header = (
        "# Generated embedded payload. Do not commit. Treat the built EXE like a credential:\n"
        "# anyone holding the binary can recover the token. No encryption is used.\n"
    )
    return header + body
