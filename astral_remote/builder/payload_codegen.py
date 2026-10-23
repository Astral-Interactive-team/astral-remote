# Embedded payload code generation for single-file distribution.
# The builder writes `_embedded_payload.py` into an isolated staging
# package copy only; the real source tree never holds generated secret
# material. PyInstaller bundles the staged module as code, so no
# standalone plaintext token file is extracted at runtime beyond normal
# bundle contents. The token remains extractable from the executable;
# this module provides no encryption theater.

from __future__ import annotations

from astral_remote.agent.payload import PAYLOAD_VERSION
from astral_remote.shared.config import validate_config
from astral_remote.shared.errors import BuildError

MAX_PAYLOAD_BYTES = 65536


def render_payload_module(config: dict) -> str:
    """Render non-secret _embedded_payload.py source for one EXE build."""
    problems = validate_config(config)
    if problems:
        raise BuildError(f"Payload config invalid: {problems[0]}")
    body = f"PAYLOAD_V = {PAYLOAD_VERSION}\nCONFIG = {config!r}\n"
    if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise BuildError("Payload too large.")
    header = (
        "# Generated non-secret runtime configuration. Do not commit.\n"
    )
    return header + body
