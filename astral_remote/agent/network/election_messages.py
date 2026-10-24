"""Election message construction and validation for authenticated peer sessions."""

import base64
import hashlib
import json
from typing import Any

from astral_remote.agent.network.authentication import _sign
from astral_remote.agent.network.constants import NETWORK_NAME, PROTOCOL_VERSION
from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.security import NodeIdentity


def membership_fingerprint(members: tuple[ElectionMember, ...]) -> str:
    """Deterministic fingerprint of the configured election membership.

    Sorted by (installation_id, node_id) and encoded as a compact canonical
    JSON array of pairs before SHA-256 hashing.
    """
    if not members:
        return ""
    pairs = sorted(
        [m.installation_id, m.node_id] for m in members
    )
    canon = json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def build_election_request_payload(
    identity: NodeIdentity,
    session_id: str,
    term: int,
    candidate_node_id: str,
    members_fingerprint: str,
) -> dict[str, Any]:
    """Build a signed election vote request."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "election-request",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "electionTerm": term,
        "candidateNodeId": candidate_node_id,
        "membershipFingerprint": members_fingerprint,
    }
    payload["signature"] = _sign(identity, payload)
    return payload


def build_election_response_payload(
    identity: NodeIdentity,
    session_id: str,
    term: int,
    candidate_node_id: str,
    granted: bool,
    members_fingerprint: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a signed election vote response."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "election-response",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "electionTerm": term,
        "candidateNodeId": candidate_node_id,
        "voteGranted": granted,
        "membershipFingerprint": members_fingerprint,
    }
    if reason is not None:
        payload["reason"] = str(reason)
    payload["signature"] = _sign(identity, payload)
    return payload


def build_lease_renewal_request_payload(
    identity: NodeIdentity,
    session_id: str,
    term: int,
    host_node_id: str,
    members_fingerprint: str,
) -> dict[str, Any]:
    """Build a signed Host authority lease renewal request."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "lease-renewal-request",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "electionTerm": term,
        "hostNodeId": host_node_id,
        "membershipFingerprint": members_fingerprint,
    }
    payload["signature"] = _sign(identity, payload)
    return payload


def build_lease_renewal_response_payload(
    identity: NodeIdentity,
    session_id: str,
    term: int,
    host_node_id: str,
    granted: bool,
    members_fingerprint: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a signed Host authority lease renewal response."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "lease-renewal-response",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "electionTerm": term,
        "hostNodeId": host_node_id,
        "leaseGranted": granted,
        "membershipFingerprint": members_fingerprint,
    }
    if reason is not None:
        payload["reason"] = str(reason)
    payload["signature"] = _sign(identity, payload)
    return payload


def build_agent_command_payload(
    identity: NodeIdentity,
    session_id: str,
    term: int,
    host_node_id: str,
    target_node_id: str,
    command: str,
    command_id: str,
    args: dict[str, Any] | None = None,
    *,
    lease_expires_at: float,
) -> dict[str, Any]:
    """Build a signed authoritative Host-to-Agent command.

    ``lease_expires_at`` is the absolute wall-clock Unix timestamp (seconds,
    from ``time.time()``) at which the Host's authority lease expires.  It is
    included in the signed payload so the receiving Agent can reject commands
    whose lease has passed, even when the Agent has not yet observed a newer
    election term.  The field is covered by the Ed25519 signature; tampering
    with it invalidates the signature.

    Callers must derive ``lease_expires_at`` from the Host's active lease and
    must never supply a timestamp later than that lease's recorded expiry.

    Note: Stale-command rejection on the receiver is conditional on the
    receiver's wall clock not lagging behind the Host's wall clock by more
    than MAX_CLOCK_SKEW_SECONDS (5.0 s). Structurally valid timestamps do
    not prove that clocks are synchronized.
    """
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "agent-command",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "hostTerm": term,
        "hostNodeId": host_node_id,
        "targetNodeId": target_node_id,
        "command": command,
        "commandId": command_id,
        "args": dict(args or {}),
        "leaseExpiresAt": lease_expires_at,
    }
    payload["signature"] = _sign(identity, payload)
    return payload


def build_agent_command_reply_payload(
    identity: NodeIdentity,
    session_id: str,
    command_id: str,
    term: int,
    success: bool,
    result: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a signed Agent command execution reply."""
    payload = {
        "version": PROTOCOL_VERSION,
        "network": NETWORK_NAME,
        "type": "agent-command-reply",
        "nodeId": identity.node_id,
        "publicKey": base64.b64encode(identity.public_key).decode("ascii"),
        "sessionId": session_id,
        "commandId": command_id,
        "hostTerm": term,
        "success": success,
    }
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = str(error)
    payload["signature"] = _sign(identity, payload)
    return payload

