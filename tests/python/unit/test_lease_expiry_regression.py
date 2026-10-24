"""Regression tests for Phase 07 receiver-side lease expiration fix.

These tests verify that a delayed agent-command is rejected after its Host
authority lease has expired, even when the receiving Agent has not observed a
newer election term.  Each test uses deterministic clocks injected via the
existing wall_clock parameter on PeerNetwork.

Clock-skew assumption: MAX_CLOCK_SKEW_SECONDS (5.0 s) is subtracted from
leaseExpiresAt before comparing to time.time().  A command near the boundary
is therefore only accepted while time.time() < leaseExpiresAt - 5.0.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from astral_remote.agent.network.constants import MAX_CLOCK_SKEW_SECONDS
from astral_remote.agent.network.election_messages import build_agent_command_payload
from astral_remote.agent.network.errors import PeerProtocolError
from astral_remote.agent.network.host_presence import HOST_CONFLICT, PRE_HOST
from astral_remote.agent.network.host_role import (
    ACTIVE,
    STANDBY,
    HostAuthorityConfig,
    HostLifecycleManager,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.agent.peer_network import PeerNetwork, PeerSession
from astral_remote.agent.runtime import AgentRuntime
from astral_remote.shared.security import (
    CredentialStore,
    ElectionStateStore,
    IdentityStore,
    NodeIdentity,
    TrustStore,
    XorTestProtector,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


class _Clock:
    """Deterministic injectable clock."""

    def __init__(self, initial: float = 1000.0):
        self.time = initial

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class _FakeWriter:
    def __init__(self):
        self.sent: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass

    def can_write_eof(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _identity(tmp_path, name: str = "id.bin") -> NodeIdentity:
    return IdentityStore(tmp_path / name, XorTestProtector(b"k")).load_or_create()


def _members(ident_node_id: str) -> tuple[ElectionMember, ...]:
    return (
        ElectionMember("inst-agent", ident_node_id),
        ElectionMember("inst-host", "host-node"),
        ElectionMember("inst-peer", "peer-node"),
    )


def _make_network(
    tmp_path,
    *,
    ident: NodeIdentity,
    known_term: int = 1,
    wall_clock: _Clock,
    mono_clock: _Clock,
    agent_runtime: Any = None,
) -> PeerNetwork:
    trust_store = TrustStore(tmp_path / "trust.json")
    election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
    if known_term > 0:
        election_store.observe_term(known_term)

    members = _members(ident.node_id)
    config = {
        "peerNetwork": {
            "enabled": True,
            "election": {
                "members": [
                    {"installationId": m.installation_id, "nodeId": m.node_id}
                    for m in members
                ]
            },
        }
    }

    return PeerNetwork(
        ident,
        trust_store,
        config,
        installation_id="inst-agent",
        election_state_store=election_store,
        agent_runtime=agent_runtime,
        clock=mono_clock,
        wall_clock=wall_clock,
    )


def _make_session(network: PeerNetwork) -> tuple[PeerSession, _FakeWriter]:
    writer = _FakeWriter()
    session = PeerSession(
        node_id="host-node",
        session_id="session-host-12345678901234567890",
        reader=None,
        writer=writer,
        interval=2.0,
        response_timeout=1.0,
    )
    network._sessions["host-node"] = session
    return session, writer


async def _decode_reply(writer: _FakeWriter) -> dict[str, Any]:
    reader = asyncio.StreamReader()
    reader.feed_data(b"".join(writer.sent))
    reader.feed_eof()
    from astral_remote.agent.network.codec import _receive
    return await _receive(reader)


def _cmd_msg(
    ident_node_id: str,
    *,
    term: int = 1,
    command: str = "status",
    command_id: str = "cmd-test",
    lease_expires_at: float,
) -> dict[str, Any]:
    """Build a raw agent-command dict (without real signature; bypass used in unit tests)."""
    return {
        "version": 1,
        "network": "astral-remote",
        "type": "agent-command",
        "nodeId": "host-node",
        "hostNodeId": "host-node",
        "targetNodeId": ident_node_id,
        "hostTerm": term,
        "command": command,
        "commandId": command_id,
        "leaseExpiresAt": lease_expires_at,
    }


# ---------------------------------------------------------------------------
# TC-1  Valid command received before conservative expiry boundary → accepted
# ---------------------------------------------------------------------------

def test_valid_command_before_expiry_is_accepted(tmp_path):
    """Command with leaseExpiresAt well in the future is accepted."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        agent_runtime = AgentRuntime(
            {"agentVersion": "0.1.0"},
            state_path=tmp_path / "state.json",
            hostname_provider=lambda: "TEST-PC",
        )
        await agent_runtime.start()
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
            agent_runtime=agent_runtime,
        )
        session, writer = _make_session(network)

        # leaseExpiresAt is 60 s in the future, well past the 5 s skew window
        msg = _cmd_msg(ident.node_id, lease_expires_at=wall.time + 60.0)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is True, reply.get("error")

        await agent_runtime.close()
        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-2  Command received after expiry → rejected
# ---------------------------------------------------------------------------

def test_command_after_expiry_is_rejected(tmp_path):
    """Command with leaseExpiresAt in the past is rejected."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
        )
        session, writer = _make_session(network)

        # Expired 30 s ago
        msg = _cmd_msg(ident.node_id, lease_expires_at=wall.time - 30.0)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is False
        assert "expired" in reply["error"].lower(), reply["error"]

        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-3  Same-term command rejected after expiry even without newer term
# ---------------------------------------------------------------------------

def test_same_term_command_rejected_after_expiry_without_newer_term(tmp_path):
    """Agent knows only term 1; Host A's command is stale because lease expired."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        # Agent knows term 1 only — no newer term observed
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
        )
        session, writer = _make_session(network)

        # Command term == known_term (1), but lease has expired
        msg = _cmd_msg(ident.node_id, term=1, lease_expires_at=wall.time - 1.0)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is False
        assert "expired" in reply["error"].lower(), reply["error"]

        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-4  Missing leaseExpiresAt → rejected
# ---------------------------------------------------------------------------

def test_missing_lease_expires_at_is_rejected(tmp_path):
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
        )
        session, writer = _make_session(network)

        msg = _cmd_msg(ident.node_id, lease_expires_at=wall.time + 60.0)
        del msg["leaseExpiresAt"]  # Remove the field
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is False
        assert "lease expiry" in reply["error"].lower(), reply["error"]

        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-5  Invalid types / NaN / ±Inf → rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_value", [
    None,
    "2000-01-01",
    True,
    False,
    [],
    {},
    float("nan"),
    float("inf"),
    float("-inf"),
])
def test_invalid_lease_expires_at_types_are_rejected(tmp_path, bad_value):
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path / f"id_{id(bad_value)}.bin")
        election_store = ElectionStateStore(
            tmp_path / f"el_{id(bad_value)}.bin", XorTestProtector(b"k")
        )
        election_store.observe_term(1)
        members = _members(ident.node_id)
        config = {
            "peerNetwork": {
                "enabled": True,
                "election": {
                    "members": [
                        {"installationId": m.installation_id, "nodeId": m.node_id}
                        for m in members
                    ]
                },
            }
        }
        trust_store = TrustStore(tmp_path / f"tr_{id(bad_value)}.json")
        network = PeerNetwork(
            ident, trust_store, config,
            installation_id="inst-agent",
            election_state_store=election_store,
            clock=mono, wall_clock=wall,
        )
        writer = _FakeWriter()
        session = PeerSession(
            node_id="host-node",
            session_id="session-host-12345678901234567890",
            reader=None, writer=writer, interval=2.0, response_timeout=1.0,
        )
        network._sessions["host-node"] = session

        msg = _cmd_msg(ident.node_id, lease_expires_at=wall.time + 60.0)
        msg["leaseExpiresAt"] = bad_value  # Overwrite with bad value
        await network._handle_agent_command(session, msg)

        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(writer.sent))
        reader.feed_eof()
        from astral_remote.agent.network.codec import _receive
        reply = await _receive(reader)
        assert reply["success"] is False
        assert "lease expiry" in reply["error"].lower(), (
            f"bad_value={bad_value!r}: unexpected error: {reply['error']!r}"
        )
        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-6  Clock-skew boundary: conservative policy, not accepted after safe deadline
# ---------------------------------------------------------------------------

def test_clock_skew_boundary_conservative_rejection(tmp_path):
    """Command is rejected when wall_clock is exactly at leaseExpiresAt - MAX_CLOCK_SKEW_SECONDS."""
    async def scenario():
        lease_expires_at = 2100.0
        # Wall clock is AT the conservative boundary (not accepted)
        wall = _Clock(lease_expires_at - MAX_CLOCK_SKEW_SECONDS)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
        )
        session, writer = _make_session(network)

        msg = _cmd_msg(ident.node_id, lease_expires_at=lease_expires_at)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        # At the boundary (time.time() >= leaseExpiresAt - skew) → rejected
        assert reply["success"] is False
        assert "expired" in reply["error"].lower(), reply["error"]

        await network.close()

    _run(scenario())


def test_clock_skew_boundary_one_tick_before_is_accepted(tmp_path):
    """Command is accepted when wall_clock is just under the conservative deadline."""
    async def scenario():
        lease_expires_at = 2100.0
        # Wall clock is 1 ms before the conservative deadline
        wall = _Clock(lease_expires_at - MAX_CLOCK_SKEW_SECONDS - 0.001)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        agent_runtime = AgentRuntime(
            {"agentVersion": "0.1.0"},
            state_path=tmp_path / "state.json",
            hostname_provider=lambda: "TEST-PC",
        )
        await agent_runtime.start()
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
            agent_runtime=agent_runtime,
        )
        session, writer = _make_session(network)

        msg = _cmd_msg(ident.node_id, lease_expires_at=lease_expires_at)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is True, reply.get("error")

        await agent_runtime.close()
        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-7  send_agent_command rejects when lease already expired at send time
# ---------------------------------------------------------------------------

def test_send_agent_command_refuses_when_lease_expired(tmp_path):
    """Host cannot send a command when its own lease wall-clock expiry has passed."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path, "host.bin")
        trust_store = TrustStore(tmp_path / "trust.json")
        election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
        # Do NOT pre-observe_term; begin_election() will advance term to 1.

        members = (
            ElectionMember("inst-host", ident.node_id),
            ElectionMember("inst-peer", "peer-node"),
            ElectionMember("inst-other", "other-node"),
        )
        config = {
            "peerNetwork": {
                "enabled": True,
                "election": {
                    "members": [
                        {"installationId": m.installation_id, "nodeId": m.node_id}
                        for m in members
                    ]
                },
            }
        }
        network = PeerNetwork(
            ident, trust_store, config,
            installation_id="inst-host",
            election_state_store=election_store,
            clock=mono,
            wall_clock=wall,
        )

        from astral_remote.agent.network.election import ELECTION_ELECTED
        from astral_remote.shared.security import CredentialStore
        coordinator = network._election_protocol.coordinator
        snap = coordinator.begin_election()
        coordinator.record_vote("peer-node", snap.term, True)
        assert coordinator.snapshot().phase == ELECTION_ELECTED

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")
        network._host_lifecycle._credential_store = cred_store

        # Record lease: expires in 6 s monotonic, same wall-clock equivalent
        network._host_lifecycle.record_initial_lease(1, mono.time)
        await network._host_lifecycle.activate()

        assert network.is_host_authoritative() is True
        # Advance wall clock past the safe send boundary
        wall.advance(network._host_lifecycle.wall_clock_lease_expires_at - wall.time + 1.0)

        # Attach a fake peer session
        writer = _FakeWriter()
        peer_session = PeerSession(
            node_id="peer-node",
            session_id="session-peer-12345678901234567890",
            reader=None, writer=writer, interval=2.0, response_timeout=1.0,
        )
        network._sessions["peer-node"] = peer_session

        with pytest.raises(PeerProtocolError, match="expired"):
            await network.send_agent_command("peer-node", "status")

        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC-8  Renewed lease cannot cause already-created command to outlive deadline
# ---------------------------------------------------------------------------

def test_lease_renewal_does_not_extend_already_issued_command_deadline(tmp_path):
    """A command payload records the expiry at the moment it was built.
    Subsequent lease renewal does not retroactively extend that deadline.
    """
    # This is a unit test on build_agent_command_payload directly:
    # the leaseExpiresAt baked in at construction time is immutable.
    from astral_remote.agent.network.election_messages import build_agent_command_payload

    ident = _identity(tmp_path)
    wall = _Clock(2000.0)

    original_expiry = wall.time + 6.0  # lease expires at 2006

    payload = build_agent_command_payload(
        ident,
        session_id="session-id-12345678901234567890",
        term=1,
        host_node_id=ident.node_id,
        target_node_id="target-node",
        command="status",
        command_id="cmd-immutable",
        lease_expires_at=original_expiry,
    )
    assert payload["leaseExpiresAt"] == original_expiry

    # Even if we advance the wall clock to simulate renewal, the payload value is unchanged
    wall.advance(3.0)
    renewed_expiry = wall.time + 6.0  # renewed to 2009
    assert payload["leaseExpiresAt"] == original_expiry, (
        "Command payload leaseExpiresAt must not be changed by renewal"
    )
    assert payload["leaseExpiresAt"] < renewed_expiry, (
        "Renewed expiry is later than the original command's deadline"
    )


# ---------------------------------------------------------------------------
# TC-9  leaseExpiresAt is covered by the Ed25519 signature
# ---------------------------------------------------------------------------

def test_lease_expires_at_is_covered_by_signature(tmp_path):
    """Tampering with leaseExpiresAt invalidates the Ed25519 signature."""
    from astral_remote.agent.network.authentication import _verify
    from astral_remote.agent.network.errors import PeerProtocolError

    ident = _identity(tmp_path)
    payload = build_agent_command_payload(
        ident,
        session_id="session-id-12345678901234567890",
        term=1,
        host_node_id=ident.node_id,
        target_node_id="target-node",
        command="status",
        command_id="cmd-sig-test",
        lease_expires_at=9999999.0,
    )
    assert "leaseExpiresAt" in payload
    assert "signature" in payload

    # Tampering: change leaseExpiresAt after signing
    tampered = dict(payload)
    tampered["leaseExpiresAt"] = 1.0  # attacker tries to make it look expired

    with pytest.raises(PeerProtocolError, match="signature"):
        _verify(tampered)


# ---------------------------------------------------------------------------
# TC-10  Existing checks (term, conflict, membership) remain intact
# ---------------------------------------------------------------------------

def test_stale_term_still_rejected_regardless_of_expiry(tmp_path):
    """Term check fires before expiry check: stale term is still rejected."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        network = _make_network(
            tmp_path, ident=ident, known_term=5,  # Agent knows term 5
            wall_clock=wall, mono_clock=mono,
        )
        session, writer = _make_session(network)

        # Term 1 < known term 5, even with valid expiry
        msg = _cmd_msg(ident.node_id, term=1, lease_expires_at=wall.time + 60.0)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is False
        assert "stale" in reply["error"].lower(), reply["error"]

        await network.close()

    _run(scenario())


def test_host_conflict_still_rejected_with_valid_expiry(tmp_path):
    """HOST_CONFLICT fencing fires before expiry check."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
        )
        # Force conflict state
        network._host_tracker._state = HOST_CONFLICT
        session, writer = _make_session(network)

        msg = _cmd_msg(ident.node_id, lease_expires_at=wall.time + 60.0)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is False
        assert "conflict" in reply["error"].lower(), reply["error"]

        await network.close()

    _run(scenario())


def test_unauthorized_command_still_rejected_with_valid_expiry(tmp_path):
    """Unknown command is still rejected even with a valid leaseExpiresAt."""
    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)
        ident = _identity(tmp_path)
        network = _make_network(
            tmp_path, ident=ident, known_term=1,
            wall_clock=wall, mono_clock=mono,
        )
        session, writer = _make_session(network)

        msg = _cmd_msg(ident.node_id, command="self-destruct", lease_expires_at=wall.time + 60.0)
        await network._handle_agent_command(session, msg)

        reply = await _decode_reply(writer)
        assert reply["success"] is False
        assert "Unauthorized" in reply["error"], reply["error"]

        await network.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# TC: wall_clock_lease_expires_at is consistent with monotonic lease duration
# ---------------------------------------------------------------------------

def test_wall_clock_lease_expires_at_consistent_with_duration(tmp_path):
    """wall_clock_lease_expires_at reflects the full lease duration from wall time."""
    from astral_remote.agent.network.election import ELECTION_ELECTED

    async def scenario():
        wall = _Clock(2000.0)
        mono = _Clock(1000.0)

        ident = _identity(tmp_path, "host.bin")
        trust_store = TrustStore(tmp_path / "trust.json")
        election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
        members = (
            ElectionMember("inst-host", ident.node_id),
            ElectionMember("inst-peer", "peer-node"),
            ElectionMember("inst-other", "other-node"),
        )
        config = {
            "peerNetwork": {
                "enabled": True,
                "election": {
                    "members": [
                        {"installationId": m.installation_id, "nodeId": m.node_id}
                        for m in members
                    ],
                    "leaseDurationSeconds": 6.0,
                },
            }
        }
        network = PeerNetwork(
            ident, trust_store, config,
            installation_id="inst-host",
            election_state_store=election_store,
            clock=mono,
            wall_clock=wall,
        )
        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")
        network._host_lifecycle._credential_store = cred_store

        coordinator = network._election_protocol.coordinator
        coordinator.begin_election()
        coordinator.record_vote("peer-node", 1, True)

        network._host_lifecycle.record_initial_lease(1, mono.time)

        # wall_clock_lease_expires_at should be wall.time + 6.0
        expected_wall_expiry = wall.time + 6.0
        actual = network._host_lifecycle.wall_clock_lease_expires_at
        assert abs(actual - expected_wall_expiry) < 0.001, (
            f"Expected wall expiry ~{expected_wall_expiry}, got {actual}"
        )

        await network.close()

    _run(scenario())


def test_lease_renewal_updates_wall_clock_expiry(tmp_path):
    """Successful lease renewal extends wall_clock_lease_expires_at."""
    wall = _Clock(2000.0)
    mono = _Clock(1000.0)

    from astral_remote.agent.network.election import ELECTION_ELECTED
    members = (
        ElectionMember("inst-0", "node-0"),
        ElectionMember("inst-1", "node-1"),
        ElectionMember("inst-2", "node-2"),
    )
    election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
    from astral_remote.agent.network.election import ElectionCoordinator
    coordinator = ElectionCoordinator("node-0", members, election_store)
    coordinator.begin_election()
    coordinator.record_vote("node-1", 1, True)

    cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
    cred_store.save_token("test-bot-token")

    manager = HostLifecycleManager(
        local_node_id="node-0",
        local_installation_id="inst-0",
        members=members,
        state_store=election_store,
        coordinator=coordinator,
        config=HostAuthorityConfig(lease_duration_seconds=6.0, renewal_interval_seconds=2.0),
        clock=mono,
        wall_clock=wall,
        credential_store=cred_store,
    )

    manager.record_initial_lease(1, mono.time)
    initial_wall_expiry = manager.wall_clock_lease_expires_at

    # Advance time and renew
    mono.advance(2.0)
    wall.advance(2.0)
    manager._role = ACTIVE  # Simulate active state for renew_lease
    manager.renew_lease(2)  # quorum = 2

    renewed_wall_expiry = manager.wall_clock_lease_expires_at
    assert renewed_wall_expiry > initial_wall_expiry, (
        "Renewed wall-clock expiry should be later than initial"
    )
    # Should be approximately wall.time + 6.0
    assert abs(renewed_wall_expiry - (wall.time + 6.0)) < 0.001
