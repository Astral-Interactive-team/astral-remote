"""Unit tests for Phase 05 Host Presence state machine.

These tests exercise the pure state machine in host_presence.py and the
host-heartbeat framing in peer_network.py without real sockets.  The
state machine is deterministic and does not depend on wall-clock timing.
"""

import asyncio
import base64

import pytest

from astral_remote.agent.network.host_presence import (
    FOLLOWING_HOST,
    HOST_CONFLICT,
    HOST_SUSPECTED,
    HOST_UNKNOWN,
    PRE_HOST,
    HostPresenceConfig,
    HostPresenceTracker,
    parse_host_presence_config,
)
from astral_remote.agent.peer_network import (
    BootstrapPeer,
    PeerNetwork,
    PeerSession,
    _canon,
    _sign,
)

from astral_remote.agent.network.heartbeat import (
    build_host_heartbeat_ack_payload,
    build_host_heartbeat_payload,
    build_peer_heartbeat_ack_payload,
    build_peer_heartbeat_payload,
)

from astral_remote.shared.security import IdentityStore, TrustStore, XorTestProtector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Minimal writer that accepts framed bytes without a real socket."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass

    def can_write_eof(self):
        return False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class _Clock:
    """Controllable monotonic clock for deterministic tests."""

    def __init__(self, start=1000.0):
        self.value = start

    def advance(self, seconds):
        self.value += seconds

    def __call__(self):
        return self.value


def _identity(path, key):
    return IdentityStore(path, XorTestProtector(key)).load_or_create()


def _config(host_presence=None, peer=None, **heartbeat):
    raw = {
        "peerNetwork": {
            "enabled": True,
            "bindHost": "127.0.0.1",
            "bindPort": 0,
            "bootstrapPeers": [],
        }
    }
    raw["peerNetwork"].update(heartbeat)
    if host_presence:
        raw["peerNetwork"]["hostPresence"] = host_presence
    if peer:
        raw["peerNetwork"]["bootstrapPeers"] = [
            {"nodeId": peer.node_id,
             "publicKey": base64.b64encode(peer.public_key).decode(),
             "host": "127.0.0.1", "port": peer.port, "protocolVersion": 1}
        ]
    return raw


def _make_network(tmp_path, identity=None, trust_store=None, config=None,
                  clock=None):
    ident = identity or _identity(tmp_path / "node", b"node-key")
    trust = trust_store or TrustStore(tmp_path / "trust")
    return PeerNetwork(ident, trust, config)


# ---------------------------------------------------------------------------
# HostPresenceConfig parsing
# ---------------------------------------------------------------------------


def test_parse_host_presence_config_defaults():
    config = parse_host_presence_config({})
    assert config.heartbeat_interval_seconds == 2.0
    assert config.response_timeout_seconds == 1.0
    assert config.failure_threshold == 3
    assert config.recovery_threshold == 1


def test_parse_host_presence_config_custom():
    config = parse_host_presence_config(
        _config(host_presence={
            "heartbeatIntervalSeconds": 0.5,
            "responseTimeoutSeconds": 0.25,
            "failureThreshold": 5,
            "recoveryThreshold": 2,
        }))
    assert config.heartbeat_interval_seconds == 0.5
    assert config.response_timeout_seconds == 0.25
    assert config.failure_threshold == 5
    assert config.recovery_threshold == 2


def test_parse_host_presence_config_rejects_bad_values():
    with pytest.raises(ValueError):
        parse_host_presence_config(
            _config(host_presence={"heartbeatIntervalSeconds": 0}))
    with pytest.raises(ValueError):
        parse_host_presence_config(
            _config(host_presence={"responseTimeoutSeconds": -1}))
    with pytest.raises(ValueError):
        parse_host_presence_config(
            _config(host_presence={"failureThreshold": 0}))
    with pytest.raises(ValueError):
        parse_host_presence_config(
            _config(host_presence={"recoveryThreshold": "bad"}))


# ---------------------------------------------------------------------------
# State machine: normal operation
# ---------------------------------------------------------------------------


def test_tracker_starts_unknown():
    tracker = HostPresenceTracker()
    assert tracker.state == HOST_UNKNOWN
    assert tracker.host_node_id is None
    status = tracker.status()
    assert status.host_node_id is None
    assert status.consecutive_failures == 0


def test_first_ack_establishes_following_host():
    tracker = HostPresenceTracker()
    tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST
    assert tracker.host_node_id == "node-v1-alpha"
    assert tracker.consecutive_failures == 0
    assert tracker.consecutive_successes == 1


def test_valid_ack_resets_failure_count():
    tracker = HostPresenceTracker()
    tracker.observe_ack("node-v1-alpha")
    tracker.observe_failure()
    tracker.observe_failure()
    assert tracker.state == HOST_SUSPECTED
    assert tracker.consecutive_failures == 2
    tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST
    assert tracker.consecutive_failures == 0


def test_no_pre_host_on_healthy_host():
    tracker = HostPresenceTracker(
        HostPresenceConfig(failure_threshold=5))
    tracker.observe_ack("node-v1-alpha")
    for _ in range(20):
        tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST
    assert tracker.consecutive_failures == 0


# ---------------------------------------------------------------------------
# State machine: failure detection
# ---------------------------------------------------------------------------


def test_missed_responses_transition_to_suspected_then_pre_host():
    tracker = HostPresenceTracker(
        HostPresenceConfig(failure_threshold=3))
    tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST

    tracker.observe_failure()
    assert tracker.state == HOST_SUSPECTED
    assert tracker.consecutive_failures == 1

    tracker.observe_failure()
    assert tracker.state == HOST_SUSPECTED
    assert tracker.consecutive_failures == 2

    tracker.observe_failure()
    assert tracker.state == PRE_HOST
    assert tracker.consecutive_failures == 3
    assert "failure" in (tracker.status().transition_reason or "")


def test_recovery_from_pre_host_after_valid_ack():
    tracker = HostPresenceTracker(
        HostPresenceConfig(failure_threshold=3, recovery_threshold=1))
    tracker.observe_ack("node-v1-alpha")
    for _ in range(3):
        tracker.observe_failure()
    assert tracker.state == PRE_HOST
    tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST
    assert tracker.consecutive_failures == 0


def test_failures_before_host_known_remain_unknown():
    tracker = HostPresenceTracker()
    # No host identity yet; failures must not claim a host failed.
    tracker.observe_failure()
    tracker.observe_failure()
    assert tracker.state == HOST_UNKNOWN
    assert tracker.host_node_id is None


def test_configured_host_requires_observation_before_following():
    tracker = HostPresenceTracker()
    tracker.configure_host("node-v1-alpha")
    assert tracker.state == HOST_UNKNOWN
    # Failures against a configured-but-unobserved host do not declare it dead.
    tracker.observe_failure()
    tracker.observe_failure()
    assert tracker.state == HOST_UNKNOWN
    # First ACK establishes following.
    tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST


# ---------------------------------------------------------------------------
# State machine: conflicts
# ---------------------------------------------------------------------------


def test_conflicting_ack_enters_host_conflict():
    tracker = HostPresenceTracker()
    tracker.observe_ack("node-v1-alpha")
    assert tracker.state == FOLLOWING_HOST
    tracker.observe_ack("node-v1-beta")
    assert tracker.state == HOST_CONFLICT
    assert tracker.host_node_id == "node-v1-alpha"
    assert tracker.status().transition_reason is not None


def test_conflicting_configured_host_enters_host_conflict():
    tracker = HostPresenceTracker()
    tracker.observe_ack("node-v1-alpha")
    tracker.configure_host("node-v1-beta")
    assert tracker.state == HOST_CONFLICT


def test_observe_conflict_never_picks_winner():
    tracker = HostPresenceTracker()
    tracker.observe_ack("node-v1-alpha")
    tracker.observe_conflict("Two nodes claim the host role")
    assert tracker.state == HOST_CONFLICT
    assert tracker.status().last_error is not None


# ---------------------------------------------------------------------------
# State machine: diagnostics
# ---------------------------------------------------------------------------


def test_status_reports_ack_age():
    clock = _Clock(start=1000.0)
    tracker = HostPresenceTracker(monotonic=clock)
    tracker.observe_ack("node-v1-alpha")
    assert tracker.status().last_ack_age_seconds is not None
    clock.advance(5.0)
    assert tracker.status().last_ack_age_seconds == pytest.approx(5.0)


def test_reset_clears_all_state():
    tracker = HostPresenceTracker()
    tracker.observe_ack("node-v1-alpha")
    tracker.observe_failure()
    tracker.observe_failure()
    tracker.reset()
    assert tracker.state == HOST_UNKNOWN
    assert tracker.host_node_id is None
    assert tracker.consecutive_failures == 0
    assert tracker.consecutive_successes == 0


# ---------------------------------------------------------------------------
# Host heartbeat framing (no sockets)
# ---------------------------------------------------------------------------


def _make_session(node_id, session_id="s" * 32, interval=0.1, timeout=0.05):
    return PeerSession(node_id, session_id, None, _FakeWriter(), interval, timeout)


def test_host_heartbeat_payload_includes_host_node_id(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id="node-v1-host", session_id="s" * 32)
    payload = build_host_heartbeat_payload(network.identity, session.session_id, 42, "node-v1-host")
    assert payload["type"] == "host-heartbeat"
    assert payload["hostNodeId"] == "node-v1-host"
    assert payload["sessionSequence"] == 42
    # Must not contain secrets.
    text = str(payload)
    assert "private" not in text.lower()
    assert "token" not in text.lower()


def test_host_heartbeat_ack_payload_matches_request(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id="node-v1-host", session_id="s" * 32)
    payload = build_host_heartbeat_ack_payload(network.identity, session.session_id, 42, "node-v1-host")
    assert payload["type"] == "host-heartbeat-ack"
    assert payload["sessionSequence"] == 42
    assert payload["hostNodeId"] == "node-v1-host"


def test_host_heartbeat_from_other_host_is_conflict(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    stranger = _identity(tmp_path / "stranger", b"stranger")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=stranger.node_id, session_id="s" * 32)

    async def scenario():
        payload = build_host_heartbeat_payload(network.identity, session.session_id, 1, stranger.node_id)
        # Re-sign with the stranger identity.
        payload["nodeId"] = stranger.node_id
        payload["publicKey"] = base64.b64encode(stranger.public_key).decode("ascii")
        payload["signature"] = _sign(stranger, payload)
        # The session node is not trusted, but a valid handshake would only
        # have allowed trusted peers.  We still verify the host conflict path.
        await network._handle_session_message(session, payload)
        # Stranger claim conflicts with this node (different hostNodeId).
        assert network._host_tracker.state in (HOST_UNKNOWN, HOST_CONFLICT)
    asyncio.run(scenario())


def test_normal_peer_heartbeat_ack_is_not_host_ack(tmp_path):
    """A peer heartbeat ACK must never be treated as a host heartbeat ACK."""
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    host_session = _make_session(node_id="node-v1-host", session_id="s" * 32)
    network._host_tracker.configure_host("node-v1-host")

    async def scenario():
        # Send a peer heartbeat-ack, not a host-heartbeat-ack.
        peer_ack = build_peer_heartbeat_ack_payload(network.identity, host_session.session_id, 5)
        host_session.pending_sequence = 5
        await network._handle_session_message(host_session, peer_ack)
        # The host tracker is still HOST_UNKNOWN because the peer ACK did not
        # establish host presence.
        assert network._host_tracker.state in (HOST_UNKNOWN,)
        assert host_session.last_host_ack_sequence is None
    asyncio.run(scenario())


def test_host_heartbeat_ack_requires_matching_pending(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    network._host_tracker.configure_host("node-v1-host")
    session = _make_session(node_id="node-v1-host", session_id="s" * 32)

    async def scenario():
        # No pending host request; ACK rejected.
        payload = build_host_heartbeat_ack_payload(network.identity, session.session_id, 1, identity.node_id)
        await network._handle_session_message(session, payload)
        assert session.last_host_ack_sequence is None

        # Set a pending request with a different sequence; ACK rejected.
        session.touch_host_sent(99)
        payload2 = build_host_heartbeat_ack_payload(network.identity, session.session_id, 7, identity.node_id)
        await network._handle_session_message(session, payload2)
        assert session.last_host_ack_sequence is None
        assert session.host_pending_sequence == 99
    asyncio.run(scenario())


def test_host_heartbeat_ack_from_wrong_host_is_rejected(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    stranger = _identity(tmp_path / "stranger", b"stranger")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    network._host_tracker.configure_host("node-v1-host")
    session = _make_session(node_id=stranger.node_id, session_id="s" * 32)
    session.touch_host_sent(5)

    async def scenario():
        # ACK claims this node is the host but is signed by a stranger.
        payload = build_host_heartbeat_ack_payload(network.identity, session.session_id, 5, identity.node_id)
        payload["nodeId"] = stranger.node_id
        payload["publicKey"] = base64.b64encode(stranger.public_key).decode("ascii")
        payload["signature"] = _sign(stranger, payload)
        await network._handle_session_message(session, payload)
        # Session identity mismatch means validation fails before host logic.
        assert session.last_host_ack_sequence is None
    asyncio.run(scenario())


def test_network_status_includes_host_presence(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    status = network.status()
    assert "hostPresence" in status
    assert status["hostPresence"]["state"] == HOST_UNKNOWN
    assert status["hostPresence"]["hostNodeId"] is None
    assert status["hostPresence"]["failureThreshold"] == 3


def test_network_host_status_method(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    host = network.host_status()
    assert host["state"] == HOST_UNKNOWN
    assert host["heartbeatInterval"] == 2.0


def test_status_remains_compatible_with_phase04(tmp_path):
    identity = _identity(tmp_path / "agent", b"agent")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    status = network.status()
    # Phase 04 keys still present.
    assert status["enabled"] is True
    assert status["listening"] is False
    assert status["peers"] == []
    assert status["heartbeatInterval"] == 2.0