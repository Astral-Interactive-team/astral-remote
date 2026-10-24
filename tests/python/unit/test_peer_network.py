"""Unit tests for direct TLS P2P transport and heartbeat presence tracking."""

import asyncio
import base64

import pytest

from astral_remote.agent.peer_network import (
    BootstrapPeer,
    DISCONNECT_STATUS_SECONDS,
    MAX_MESSAGE_BYTES,
    PeerNetwork,
    PeerProtocolError,
    PeerSession,
    parse_heartbeat_config,
    parse_network_config,
)

from astral_remote.agent.network.heartbeat import (
    build_host_heartbeat_ack_payload,
    build_host_heartbeat_payload,
    build_peer_heartbeat_ack_payload,
    build_peer_heartbeat_payload,
)

from astral_remote.shared.security import IdentityStore, TrustStore, XorTestProtector


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


# The live transport is expected to be able to detect a silent peer within
# this many seconds, so the public contract keeps a value below a minute.
assert 0 < DISCONNECT_STATUS_SECONDS < 60

# A malicious or malformed peer must not be able to reach JSON decoding with
# more than the framed message budget.
assert 0 < MAX_MESSAGE_BYTES <= 64 * 1024


def _identity(path, key):
    return IdentityStore(path, XorTestProtector(key)).load_or_create()


def _config(peer=None, **heartbeat):
    raw = {"peerNetwork": {"enabled": True, "bindHost": "127.0.0.1", "bindPort": 0, "bootstrapPeers": []}}
    raw["peerNetwork"].update(heartbeat)
    if peer:
        raw["peerNetwork"]["bootstrapPeers"] = [{"nodeId": peer.node_id, "publicKey": base64.b64encode(peer.public_key).decode(), "host": "127.0.0.1", "port": peer.port, "protocolVersion": 1}]
    return raw


async def _wait_for(predicate, timeout=3.0, interval=0.02):
    """Deterministically wait until predicate() is true or raise."""
    elapsed = 0.0
    while elapsed < timeout:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError(f"Condition not met within {timeout}s: {predicate!r}")


def _make_session(node_id="node-v1-a", session_id="s" * 32, interval=2.0, timeout=1.0, writer=None):
    return PeerSession(node_id, session_id, None, writer or _FakeWriter(), interval, timeout)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_network_config_rejects_bad_bootstrap_key():
    with pytest.raises(PeerProtocolError):
        parse_network_config({"peerNetwork": {"enabled": True, "bootstrapPeers": [{"nodeId": "bad"}]}})


def test_parse_network_config():
    """Validate config parsing so disabled fallbacks cannot be skipped."""
    enabled, host, port, peers = parse_network_config(_config())
    assert (enabled, host, port, peers) == (True, "127.0.0.1", 0, ())
    enabled, host, port, peers = parse_network_config({})
    assert (enabled, host, port, peers) == (False, "127.0.0.1", None, ())
    with pytest.raises(PeerProtocolError):
        parse_network_config({"peerNetwork": "bad"})
    with pytest.raises(PeerProtocolError):
        parse_network_config({"peerNetwork": {"enabled": "yes"}})
    with pytest.raises(PeerProtocolError):
        parse_network_config({"peerNetwork": {"bindPort": "bad"}})


def test_parse_heartbeat_config():
    """Heartbeat timing must be a positive number and reject bad values."""
    interval, timeout = parse_heartbeat_config(_config())
    assert interval > 0 and timeout > 0
    interval, timeout = parse_heartbeat_config({"peerNetwork": {"heartbeatIntervalSeconds": 0.1, "heartbeatResponseTimeoutSeconds": 0.05}})
    assert (interval, timeout) == (0.1, 0.05)
    for bad in ("yes", True, 0, -1):
        with pytest.raises(PeerProtocolError):
            parse_heartbeat_config({"peerNetwork": {"heartbeatIntervalSeconds": bad}})
        with pytest.raises(PeerProtocolError):
            parse_heartbeat_config({"peerNetwork": {"heartbeatResponseTimeoutSeconds": bad}})


# ---------------------------------------------------------------------------
# Live two-node scenarios
# ---------------------------------------------------------------------------


def test_direct_heartbeat_exchange_updates_presence(tmp_path):
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server_trust, client_trust = TrustStore(tmp_path / "server-trust"), TrustStore(tmp_path / "client-trust")
        server_trust.trust(client_identity.node_id, client_identity.public_key)
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        server = PeerNetwork(server_identity, server_trust, _config())
        await server.start()
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust, _config(peer))
        await client.start()

        await _wait_for(lambda: len(client.status()["peers"]) > 0
                        and client.status()["peers"][0]["connected"]
                        and client.status()["peers"][0]["heartbeat"]["state"] in ("active", "waiting"))
        await _wait_for(lambda: len(server.status()["peers"]) > 0
                        and server.status()["peers"][0]["connected"]
                        and server.status()["peers"][0]["heartbeat"]["state"] in ("active", "waiting"))

        client_peer = client.status()["peers"][0]
        server_peer = server.status()["peers"][0]
        assert client_peer["node_id"] == server_identity.node_id
        assert server_peer["node_id"] == client_identity.node_id
        assert client_peer["heartbeat"]["state"] in ("active", "waiting")
        assert server_peer["heartbeat"]["state"] in ("active", "waiting")
        await client.close(); await server.close()
    asyncio.run(scenario())


def test_heartbeat_sequence_matches_ack(tmp_path):
    """Valid ACKs carry the same sequence as the request they answer."""
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server_trust, client_trust = TrustStore(tmp_path / "server-trust"), TrustStore(tmp_path / "client-trust")
        server_trust.trust(client_identity.node_id, client_identity.public_key)
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        server = PeerNetwork(server_identity, server_trust, _config(heartbeatIntervalSeconds=0.05, heartbeatResponseTimeoutSeconds=0.05))
        await server.start()
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust, _config(peer, heartbeatIntervalSeconds=0.05, heartbeatResponseTimeoutSeconds=0.05))
        await client.start()

        await _wait_for(lambda: len(client.status()["peers"]) > 0
                        and client.status()["peers"][0]["heartbeat"]["state"] == "active")
        await _wait_for(lambda: len(server.status()["peers"]) > 0
                        and server.status()["peers"][0]["heartbeat"]["state"] == "active")

        client_peer = client.status()["peers"][0]
        server_peer = server.status()["peers"][0]
        assert client_peer["heartbeat"]["last_ack_sequence"] is not None
        assert server_peer["heartbeat"]["last_ack_sequence"] is not None
        assert server_peer["heartbeat"]["last_heartbeat_age_seconds"] is not None
        assert server_peer["heartbeat"]["last_ack_age_seconds"] is not None

        await client.close(); await server.close()
    asyncio.run(scenario())


def test_direct_tls_connection_requires_mutual_trust(tmp_path):
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server_trust, client_trust = TrustStore(tmp_path / "server-trust"), TrustStore(tmp_path / "client-trust")
        server_trust.trust(client_identity.node_id, client_identity.public_key)
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        server = PeerNetwork(server_identity, server_trust, _config())
        await server.start()
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust, _config(peer))
        await client.start()
        await _wait_for(lambda: len(client.status()["peers"]) > 0
                        and client.status()["peers"][0]["connected"])
        await _wait_for(lambda: len(server.status()["peers"]) > 0)
        assert server.status()["peers"][0]["node_id"] == client_identity.node_id
        client_peer = client.status()["peers"][0]
        assert client_peer["node_id"] == server_identity.node_id
        assert client_peer["connected"] is True
        assert client_peer["transport"].casefold() == "direct-tls"
        assert server.status()["peers"][0]["node_id"] == client_identity.node_id
        assert server.status()["peers"][0]["connected"] is True
        await client.close(); await server.close()
    asyncio.run(scenario())


def test_untrusted_peer_is_not_connected(tmp_path):
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server = PeerNetwork(server_identity, TrustStore(tmp_path / "server-trust"), _config())
        await server.start()
        client_trust = TrustStore(tmp_path / "client-trust")
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust, _config(peer))
        await client.start()
        await _wait_for(lambda: len(client.status()["peers"]) > 0)
        assert client.status()["peers"][0]["connected"] is False
        await client.close(); await server.close()
    asyncio.run(scenario())


def test_hello_validation_rejects_malformed_and_mismatched_messages(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    message = network._authenticator._hello("client-hello", "n" * 44, None)
    bad_signature = dict(message)
    bad_signature["signature"] = "not-base64"
    with pytest.raises(PeerProtocolError):
        network._authenticator._validate_hello(bad_signature, "client-hello", None)
    wrong_type = network._authenticator._hello("server-hello", "n" * 44, None)
    with pytest.raises(PeerProtocolError):
        network._authenticator._validate_hello(wrong_type, "client-hello", None)
    bound = network._authenticator._hello("client-hello", "n" * 44, "n" * 44)
    bound["peerNonce"] = "different"
    with pytest.raises(PeerProtocolError):
        network._authenticator._validate_hello(bound, "client-hello", "n" * 44)


# ---------------------------------------------------------------------------
# PeerSession unit tests (no sockets needed)
# ---------------------------------------------------------------------------


def test_session_tracks_pending_sequence_and_clears_on_ack():
    session = _make_session()
    session.touch_sent(1)
    assert session.pending_sequence == 1
    assert session.pending_since is not None
    assert session.last_heartbeat_sequence == 1
    session.touch_ack(1)
    assert session.pending_sequence is None
    assert session.pending_since is None
    assert session.last_ack_sequence == 1


def test_session_tracks_received_sequence():
    session = _make_session()
    session.touch_received(3)
    assert session.last_received_sequence == 3
    assert session.last_received_time is not None


def test_session_records_error_without_secrets():
    session = _make_session()
    session.record_error("some failure")
    assert session.last_error == "some failure"
    assert session.last_error_wall is not None


def test_session_close_marks_inactive():
    session = _make_session()
    assert session.active is True
    session.close()
    assert session.active is False


# ---------------------------------------------------------------------------
# Message validation unit tests
# ---------------------------------------------------------------------------


def test_message_validation_rejects_unsupported_types(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=identity.node_id, session_id="s" * 32)
    with pytest.raises(PeerProtocolError):
        network._validate_session_message(
            {"version": 1, "network": "astral-remote", "type": "unknown", "nodeId": identity.node_id}, session, "heartbeat")


def test_message_validation_rejects_session_mismatch(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=identity.node_id, session_id="s" * 32)
    payload = build_peer_heartbeat_payload(
        network.identity,
        session.session_id,
        1,
    )
    payload["sessionId"] = "different-session"
    with pytest.raises(PeerProtocolError):
        network._validate_session_message(payload, session, "heartbeat")


def test_ack_without_pending_request_is_rejected(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=identity.node_id, session_id="s" * 32)

    async def scenario():
        payload = build_peer_heartbeat_ack_payload(network.identity, session.session_id, 1)
        await network._handle_session_message(session, payload)
        assert session.last_ack_sequence is None
        assert session.last_error is not None
    asyncio.run(scenario())


def test_ack_with_wrong_sequence_is_rejected(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=identity.node_id, session_id="s" * 32)

    async def scenario():
        session.touch_sent(5)
        payload = build_peer_heartbeat_ack_payload(network.identity, session.session_id, 99)
        await network._handle_session_message(session, payload)
        assert session.last_ack_sequence is None
        assert session.last_error is not None
        # The pending request is never cleared by a wrong ACK.
        assert session.pending_sequence == 5
    asyncio.run(scenario())


def test_duplicate_heartbeat_is_rejected(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=identity.node_id, session_id="s" * 32)

    async def scenario():
        payload = build_peer_heartbeat_payload(
            network.identity,
            session.session_id,
            2,
        )
        await network._handle_session_message(session, payload)
        assert session.last_received_sequence == 2
        assert session.last_error is None
        # Same sequence again is stale.
        await network._handle_session_message(session, payload)
        assert session.last_received_sequence == 2
        assert session.last_error is not None
    asyncio.run(scenario())


def test_invalid_sequence_type_is_rejected(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"), _config())
    session = _make_session(node_id=identity.node_id, session_id="s" * 32)

    async def scenario():
        payload = build_peer_heartbeat_payload(
            network.identity,
            session.session_id,
            1,
        )
        payload["sessionSequence"] = "not-an-int"
        await network._handle_session_message(session, payload)
        assert session.last_received_sequence is None
        assert session.last_error is not None
    asyncio.run(scenario())


def test_heartbeat_from_untrusted_session_is_rejected(tmp_path):
    """A session whose node is not in the trust store rejects its heartbeats."""
    identity = _identity(tmp_path / "node", b"node")
    trust = TrustStore(tmp_path / "trust")
    network = PeerNetwork(identity, trust, _config())
    stranger = _identity(tmp_path / "stranger", b"stranger")
    # Session node is not trusted.
    session = _make_session(node_id=stranger.node_id, session_id="s" * 32)

    async def scenario():
        payload = build_peer_heartbeat_payload(
            network.identity,
            session.session_id,
            1,
        )
        # Replace sender identity with the stranger's key so signature verifies,
        # but the session node_id still mismatches the trusted set.
        payload["nodeId"] = stranger.node_id
        payload["publicKey"] = base64.b64encode(stranger.public_key).decode("ascii")
        # Re-sign with the stranger's identity.
        from astral_remote.agent.peer_network import _canon, _sign
        payload["signature"] = _sign(stranger, payload)
        await network._handle_session_message(session, payload)
        assert session.last_received_sequence is None
        assert session.last_error is not None
    asyncio.run(scenario())


def test_heartbeat_receipt_does_not_auto_trust(tmp_path):
    """A valid heartbeat from an unknown node never registers presence."""
    identity = _identity(tmp_path / "node", b"node")
    trust = TrustStore(tmp_path / "trust")
    network = PeerNetwork(identity, trust, _config())
    stranger = _identity(tmp_path / "stranger", b"stranger")
    # Explicitly NOT trusting stranger. Its session is rejected at handshake,
    # so a forged session cannot create a presence entry either.
    session = _make_session(node_id=stranger.node_id, session_id="s" * 32)

    async def scenario():
        payload = build_peer_heartbeat_payload(
            network.identity,
            session.session_id,
            1,
        )
        payload["nodeId"] = stranger.node_id
        payload["publicKey"] = base64.b64encode(stranger.public_key).decode("ascii")
        from astral_remote.agent.peer_network import _sign
        payload["signature"] = _sign(stranger, payload)
        await network._handle_session_message(session, payload)
        # Session identity does not match the current node, so validation
        # fails and no heartbeat is recorded.
        assert session.last_received_sequence is None
        assert trust.get(stranger.node_id) is None
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Heartbeat timeout and state behavior (fast, deterministic)
# ---------------------------------------------------------------------------


def test_heartbeat_state_is_waiting_before_first_ack(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"),
                          _config(heartbeatIntervalSeconds=0.02, heartbeatResponseTimeoutSeconds=0.01))
    session = _make_session(node_id=identity.node_id, session_id="s" * 32,
                            interval=0.02, timeout=0.01)
    session.touch_sent(1)
    status = network._status_for(session)
    assert status.heartbeat.state == "waiting"
    assert status.connected is True
    assert status.heartbeat.sequence == 1


def test_heartbeat_state_is_active_after_ack(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"),
                          _config(heartbeatIntervalSeconds=0.02, heartbeatResponseTimeoutSeconds=0.01))
    session = _make_session(node_id=identity.node_id, session_id="s" * 32,
                            interval=0.02, timeout=0.01)
    session.touch_sent(1)
    session.touch_ack(1)
    status = network._status_for(session)
    assert status.heartbeat.state == "active"
    assert status.heartbeat.last_ack_sequence == 1


def test_heartbeat_state_is_stale_after_disconnect_threshold(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"),
                          _config(heartbeatIntervalSeconds=0.02, heartbeatResponseTimeoutSeconds=0.01))
    session = _make_session(node_id=identity.node_id, session_id="s" * 32,
                            interval=0.02, timeout=0.01)
    # Simulate an old ACK far beyond the disconnect threshold.
    session.touch_sent(1)
    session.touch_ack(1)
    session.last_ack_time -= DISCONNECT_STATUS_SECONDS + 1
    status = network._status_for(session)
    assert status.heartbeat.state == "stale"
    # The socket is still alive even though presence is stale.
    assert status.connected is True


def test_heartbeat_timeout_retries_then_closes(tmp_path):
    """A heartbeat loop with no ACK re-sends and then tears down."""
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"),
                          _config(heartbeatIntervalSeconds=0.01, heartbeatResponseTimeoutSeconds=0.01))
    session = _make_session(node_id=identity.node_id, session_id="s" * 32,
                            interval=0.01, timeout=0.01)

    async def scenario():
        # Run the heartbeat loop; it should re-send and then break because
        # no ACK will ever arrive (no reader loop to feed it).
        await network._run_heartbeat(session)
        assert session.last_error is not None
        # The writer received at least the first heartbeat plus retries.
        assert len(session.writer.sent) >= 1
        # The owning session task would call _shutdown_session afterwards.
        session.close()
        assert session.active is False
    asyncio.run(scenario())


def test_heartbeat_timeout_marks_stale_before_ack(tmp_path):
    """If pending_since is older than DISCONNECT_STATUS_SECONDS, state is stale."""
    identity = _identity(tmp_path / "node", b"node")
    network = PeerNetwork(identity, TrustStore(tmp_path / "trust"),
                          _config(heartbeatIntervalSeconds=0.02, heartbeatResponseTimeoutSeconds=0.01))
    session = _make_session(node_id=identity.node_id, session_id="s" * 32,
                            interval=0.02, timeout=0.01)
    session.touch_sent(1)
    session.pending_since -= DISCONNECT_STATUS_SECONDS + 1
    status = network._status_for(session)
    assert status.heartbeat.state == "stale"
    assert status.connected is True


def test_peer_reconnect_starts_new_session(tmp_path):
    """After a disconnect, a new connection creates a fresh session."""
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server_trust, client_trust = TrustStore(tmp_path / "server-trust"), TrustStore(tmp_path / "client-trust")
        server_trust.trust(client_identity.node_id, client_identity.public_key)
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        server = PeerNetwork(server_identity, server_trust,
                             _config(heartbeatIntervalSeconds=0.04, heartbeatResponseTimeoutSeconds=0.04))
        await server.start()
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust,
                             _config(peer, heartbeatIntervalSeconds=0.04, heartbeatResponseTimeoutSeconds=0.04))
        await client.start()
        await _wait_for(lambda: len(client.status()["peers"]) > 0
                        and client.status()["peers"][0]["connected"]
                        and client.status()["peers"][0]["heartbeat"]["state"] in ("active", "waiting"))
        # Close client to drop the session.
        await client.close()
        await _wait_for(lambda: len(server.status()["peers"]) == 0
                        or not server.status()["peers"][0]["connected"], timeout=2.0)
        # Recreate client and reconnect.
        client2 = PeerNetwork(client_identity, client_trust,
                              _config(peer, heartbeatIntervalSeconds=0.04, heartbeatResponseTimeoutSeconds=0.04))
        await client2.start()
        await _wait_for(lambda: len(client2.status()["peers"]) > 0
                        and client2.status()["peers"][0]["connected"]
                        and client2.status()["peers"][0]["heartbeat"]["state"] in ("active", "waiting"))
        await client2.close(); await server.close()
    asyncio.run(scenario())


def test_one_peer_failure_does_not_stop_others(tmp_path):
    """A failing peer session never takes down the network or other peers."""
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server_trust, client_trust = TrustStore(tmp_path / "server-trust"), TrustStore(tmp_path / "client-trust")
        server_trust.trust(client_identity.node_id, client_identity.public_key)
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        server = PeerNetwork(server_identity, server_trust,
                             _config(heartbeatIntervalSeconds=0.04, heartbeatResponseTimeoutSeconds=0.04))
        await server.start()
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust,
                             _config(peer, heartbeatIntervalSeconds=0.04, heartbeatResponseTimeoutSeconds=0.04))
        await client.start()
        await _wait_for(lambda: len(client.status()["peers"]) > 0
                        and client.status()["peers"][0]["connected"])
        # Kill the client; the server should keep listening and still be
        # able to accept a new connection afterwards.
        await client.close()
        await _wait_for(lambda: len(server.status()["peers"]) == 0
                        or not server.status()["peers"][0]["connected"], timeout=2.0)
        assert server.status()["listening"] is True
        client2 = PeerNetwork(client_identity, client_trust,
                              _config(peer, heartbeatIntervalSeconds=0.04, heartbeatResponseTimeoutSeconds=0.04))
        await client2.start()
        await _wait_for(lambda: len(client2.status()["peers"]) > 0
                        and client2.status()["peers"][0]["connected"])
        await client2.close(); await server.close()
    asyncio.run(scenario())

def test_election_membership_accepts_matching_local_node(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    config = _config()
    config["peerNetwork"]["election"] = {
        "members": [
            {
                "installationId": "install-a",
                "nodeId": identity.node_id,
            }
        ]
    }

    network = PeerNetwork(
        identity,
        TrustStore(tmp_path / "trust"),
        config,
        installation_id="install-a",
    )

    assert network.installation_id == "install-a"


def test_election_membership_rejects_missing_installation_id(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    config = _config()
    config["peerNetwork"]["election"] = {
        "members": [
            {
                "installationId": "install-a",
                "nodeId": identity.node_id,
            }
        ]
    }

    with pytest.raises(
        PeerProtocolError,
        match="Election membership requires an installation ID",
    ):
        PeerNetwork(
            identity,
            TrustStore(tmp_path / "trust"),
            config,
        )


def test_election_membership_rejects_unregistered_installation(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    config = _config()
    config["peerNetwork"]["election"] = {
        "members": [
            {
                "installationId": "install-a",
                "nodeId": identity.node_id,
            }
        ]
    }

    with pytest.raises(
        PeerProtocolError,
        match="Local node does not match election membership",
    ):
        PeerNetwork(
            identity,
            TrustStore(tmp_path / "trust"),
            config,
            installation_id="install-b",
        )


def test_election_membership_rejects_node_id_mismatch(tmp_path):
    identity = _identity(tmp_path / "node", b"node")
    config = _config()
    config["peerNetwork"]["election"] = {
        "members": [
            {
                "installationId": "install-a",
                "nodeId": "node-v1-not-the-local-node",
            }
        ]
    }

    with pytest.raises(
        PeerProtocolError,
        match="Local node does not match election membership",
    ):
        PeerNetwork(
            identity,
            TrustStore(tmp_path / "trust"),
            config,
            installation_id="install-a",
        )


def test_peer_network_status_includes_election_when_configured(tmp_path):
    from astral_remote.shared.security import ElectionStateStore

    identity = _identity(tmp_path / "node", b"node")
    store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"test-key"))
    config = _config()
    config["peerNetwork"]["election"] = {
        "members": [
            {
                "installationId": "install-a",
                "nodeId": identity.node_id,
            }
        ]
    }

    network = PeerNetwork(
        identity,
        TrustStore(tmp_path / "trust"),
        config,
        installation_id="install-a",
        election_state_store=store,
    )

    status = network.status()
    assert "election" in status
    assert status["election"]["term"] == 0
    assert status["election"]["phase"] == "IDLE"
    assert status["election"]["quorum"] == 1
    assert "membershipFingerprint" in status["election"]


def test_peer_network_handles_signed_election_request(tmp_path):
    from astral_remote.agent.network.election_messages import (
        build_election_request_payload,
        membership_fingerprint,
    )
    from astral_remote.agent.network.models import ElectionMember
    from astral_remote.shared.security import ElectionStateStore

    voter_identity = _identity(tmp_path / "voter", b"voter-key")
    cand_identity = _identity(tmp_path / "cand", b"cand-key")
    voter_store = ElectionStateStore(
        tmp_path / "election.bin", XorTestProtector(b"test-key")
    )

    members = (
        ElectionMember("install-voter", voter_identity.node_id),
        ElectionMember("install-cand", cand_identity.node_id),
    )
    config = _config()
    config["peerNetwork"]["election"] = {
        "members": [
            {"installationId": m.installation_id, "nodeId": m.node_id}
            for m in members
        ]
    }

    voter_trust = TrustStore(tmp_path / "trust")
    voter_trust.trust(cand_identity.node_id, cand_identity.public_key)

    network = PeerNetwork(
        voter_identity,
        voter_trust,
        config,
        installation_id="install-voter",
        election_state_store=voter_store,
    )

    writer = _FakeWriter()
    session_id = "s" * 32
    session = _make_session(
        node_id=cand_identity.node_id,
        session_id=session_id,
        writer=writer,
    )
    network._sessions[cand_identity.node_id] = session

    fp = membership_fingerprint(members)
    payload = build_election_request_payload(
        cand_identity,
        session_id,
        term=1,
        candidate_node_id=cand_identity.node_id,
        members_fingerprint=fp,
    )

    async def scenario():
        await network._handle_session_message(session, payload)
        assert len(writer.sent) == 1
        assert voter_store.load().voted_for == cand_identity.node_id

    asyncio.run(scenario())


def test_host_monitor_failure_automatically_triggers_election(tmp_path):
    from astral_remote.shared.security import ElectionStateStore

    identity = _identity(tmp_path / "node", b"node")
    store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"test-key"))
    config = _config(
        heartbeatIntervalSeconds=0.02,
        heartbeatResponseTimeoutSeconds=0.01,
    )
    config["peerNetwork"]["hostPresence"] = {
        "heartbeatIntervalSeconds": 0.02,
        "responseTimeoutSeconds": 0.01,
        "failureThreshold": 1,
    }
    config["peerNetwork"]["election"] = {
        "members": [
            {
                "installationId": "install-a",
                "nodeId": identity.node_id,
            },
            {
                "installationId": "install-b",
                "nodeId": "node-b",
            },
        ],
        "timeoutSeconds": 0.5,
        "retryDelaySeconds": 0.05,
    }

    network = PeerNetwork(
        identity,
        TrustStore(tmp_path / "trust"),
        config,
        installation_id="install-a",
        election_state_store=store,
    )

    host_session = _make_session(node_id="node-b", session_id="s" * 32, writer=_FakeWriter())
    network._host_tracker.observe_ack("node-b")
    assert network.host_status()["state"] == "FOLLOWING_HOST"
    network._host_session = host_session

    async def scenario():
        monitor_task = asyncio.create_task(network._run_host_monitor(host_session))
        try:
            await _wait_for(
                lambda: network.election_snapshot() is not None
                and network.election_snapshot().phase == "CANDIDATE"
                and network.election_snapshot().term == 1,
                timeout=2.0,
            )
            assert network.host_status()["state"] == "PRE_HOST"
            assert network.election_snapshot().candidate_node_id == identity.node_id
        finally:
            monitor_task.cancel()
            await network.close()

    asyncio.run(scenario())
