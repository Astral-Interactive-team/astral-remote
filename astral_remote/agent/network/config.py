"""Configuration parsing for the peer networking subsystem."""

from typing import Any

from astral_remote.agent.network.constants import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_RESPONSE_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
)

from astral_remote.agent.network.errors import PeerProtocolError

from astral_remote.agent.network.models import (
    BootstrapPeer,
    ElectionMember,
)


def parse_network_config(config: dict[str, Any] | None) -> tuple[bool, str, int | None, tuple[BootstrapPeer, ...]]:
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PeerProtocolError("Peer network configuration is invalid.")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise PeerProtocolError("Peer network enabled must be true or false.")
    bind_host, bind_port = raw.get("bindHost", "127.0.0.1"), raw.get("bindPort")
    if not isinstance(bind_host, str) or not bind_host:
        raise PeerProtocolError("Peer network bind host is invalid.")
    if bind_port is not None and (not isinstance(bind_port, int) or not 0 <= bind_port <= 65535):
        raise PeerProtocolError("Peer network bind port is invalid.")
    peers = raw.get("bootstrapPeers", [])
    if not isinstance(peers, list):
        raise PeerProtocolError("Bootstrap peer list is invalid.")
    return enabled, bind_host, bind_port, tuple(BootstrapPeer.from_config(item) for item in peers)

def parse_heartbeat_config(config: dict[str, Any] | None) -> tuple[float, float]:
    """Validate optional peer presence timing policy.

    Both values are seconds and both use a monotonic clock at runtime; wall
    time is never used for timing decisions. The values are deliberately
    independent of the existing configured peer address and port.
    """
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PeerProtocolError("Peer network configuration is invalid.")
    interval = raw.get("heartbeatIntervalSeconds", HEARTBEAT_INTERVAL_SECONDS)
    timeout = raw.get("heartbeatResponseTimeoutSeconds", HEARTBEAT_RESPONSE_TIMEOUT_SECONDS)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise PeerProtocolError("Peer heartbeat interval is invalid.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise PeerProtocolError("Peer heartbeat response timeout is invalid.")
    return float(interval), float(timeout)

def parse_election_members(
    config: dict[str, Any] | None,
) -> tuple[ElectionMember, ...]:
    """Parse and validate the configured election membership."""
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PeerProtocolError("Peer network configuration is invalid.")

    election = raw.get("election", {})
    if election is None:
        election = {}
    if not isinstance(election, dict):
        raise PeerProtocolError("Election configuration is invalid.")

    members = election.get("members", [])
    if not isinstance(members, list):
        raise PeerProtocolError("Election member list is invalid.")

    result: list[ElectionMember] = []
    installation_ids: set[str] = set()
    node_ids: set[str] = set()

    for item in members:
        if not isinstance(item, dict):
            raise PeerProtocolError("Election member is invalid.")

        installation_id = item.get("installationId")
        node_id = item.get("nodeId")

        if (
            not isinstance(installation_id, str)
            or not installation_id.strip()
            or not isinstance(node_id, str)
            or not node_id.strip()
        ):
            raise PeerProtocolError("Election member identity is invalid.")

        installation_id = installation_id.strip()
        node_id = node_id.strip()

        if (
            installation_id in installation_ids
            or node_id in node_ids
        ):
            raise PeerProtocolError(
                "Election member identities must be unique."
            )

        installation_ids.add(installation_id)
        node_ids.add(node_id)
        result.append(
            ElectionMember(
                installation_id=installation_id,
                node_id=node_id,
            )
        )

    return tuple(result)