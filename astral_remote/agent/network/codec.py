"""Canonical message encoding and length-prefixed framing."""

import asyncio
import json
from typing import Any

from astral_remote.agent.network.constants import MAX_MESSAGE_BYTES
from astral_remote.agent.network.errors import PeerProtocolError

def _canon(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    data = _canon(message)
    if len(data) > MAX_MESSAGE_BYTES:
        raise PeerProtocolError("Peer message exceeds size limit.")
    writer.write(len(data).to_bytes(4, "big") + data)
    await writer.drain()


async def _receive(reader: asyncio.StreamReader) -> dict[str, Any]:
    length = int.from_bytes(await reader.readexactly(4), "big")
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise PeerProtocolError("Peer message exceeds size limit.")
    try:
        value = json.loads(await reader.readexactly(length))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception as exc:
        raise PeerProtocolError("Peer message is malformed.") from exc
