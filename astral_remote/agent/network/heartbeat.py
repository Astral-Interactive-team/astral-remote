import base64
from typing import Any

from astral_remote.agent.network.authentication import _sign
from astral_remote.agent.network.constants import NETWORK_NAME, PROTOCOL_VERSION
from astral_remote.shared.security import NodeIdentity


def build_peer_heartbeat_payload(
    identity: NodeIdentity,
    session_id: str,
    sequence: int,
) -> dict[str, Any]:
    """Build a signed peer heartbeat message."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "heartbeat",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "sessionSequence": sequence,
    }
    payload["signature"] = _sign(identity, payload)
    return payload


def build_peer_heartbeat_ack_payload(
    identity: NodeIdentity,
    session_id: str,
    sequence: int,
) -> dict[str, Any]:
    """Build a signed peer heartbeat acknowledgment."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "heartbeat-ack",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "sessionSequence": sequence,
    }
    payload["signature"] = _sign(identity, payload)
    return payload


def build_host_heartbeat_payload(
    identity: NodeIdentity,
    session_id: str,
    sequence: int,
    host_node_id: str,
) -> dict[str, Any]:
    """Build a signed Host heartbeat message."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "host-heartbeat",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "hostNodeId": host_node_id,
        "sessionSequence": sequence,
    }
    payload["signature"] = _sign(identity, payload)
    return payload


def build_host_heartbeat_ack_payload(
    identity: NodeIdentity,
    session_id: str,
    sequence: int,
    host_node_id: str,
) -> dict[str, Any]:
    """Build a signed Host heartbeat acknowledgment."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "host-heartbeat-ack",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "hostNodeId": host_node_id,
        "sessionSequence": sequence,
    }
    payload["signature"] = _sign(identity, payload)
    return payload