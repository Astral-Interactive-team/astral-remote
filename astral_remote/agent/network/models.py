"""Data models used by the peer networking subsystem."""

import base64
from dataclasses import dataclass
from typing import Any

from astral_remote.agent.network.constants import PROTOCOL_VERSION
from astral_remote.agent.network.errors import PeerProtocolError
from astral_remote.shared.security import SecurityStoreError, node_id_for

@dataclass(frozen=True)
class ElectionMember:
    """Identify a configured election member."""
    installation_id: str
    node_id: str

@dataclass(frozen=True)
class BootstrapPeer:
    node_id: str
    public_key: bytes
    host: str
    port: int
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_config(cls, value: dict[str, Any]) -> "BootstrapPeer":
        try:
            key = base64.b64decode(value["publicKey"], validate=True)
            node_id, host, port = value["nodeId"], value["host"], value["port"]
            version = value.get("protocolVersion", PROTOCOL_VERSION)
            if not isinstance(host, str) or not host or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError
            if not isinstance(version, int) or version != PROTOCOL_VERSION or node_id != node_id_for(key):
                raise ValueError
            return cls(node_id, key, host, port, version)
        except (KeyError, TypeError, ValueError, SecurityStoreError) as exc:
            raise PeerProtocolError("Bootstrap peer configuration is invalid.") from exc


@dataclass(frozen=True)
class PeerHeartbeat:
    """Latest validated presence evidence for one peer session."""

    state: str
    interval_seconds: float
    response_timeout_seconds: float
    sequence: int | None = None
    last_sent_at: str | None = None
    last_received_at: str | None = None
    last_ack_at: str | None = None
    last_ack_sequence: int | None = None
    last_heartbeat_age_seconds: float | None = None
    last_received_age_seconds: float | None = None
    last_ack_age_seconds: float | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class PeerStatus:
    node_id: str
    connected: bool
    transport: str
    error: str | None = None
    heartbeat: PeerHeartbeat | None = None