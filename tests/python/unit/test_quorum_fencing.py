"""Unit tests for Phase 07 quorum monitoring, authority leases, and fencing invariants."""

import asyncio
from typing import Any
import pytest

from astral_remote.agent.network.election import (
    ELECTION_ELECTED,
    ElectionCoordinator,
)
from astral_remote.agent.network.election_protocol import (
    ElectionConfig,
    ElectionProtocol,
)
from astral_remote.agent.network.host_presence import PRE_HOST
from astral_remote.agent.network.host_role import (
    ACTIVE,
    FENCED,
    STANDBY,
    HostAuthorityConfig,
    HostLifecycleManager,
    LeaseGrant,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.security import (
    CredentialStore,
    ElectionStateStore,
    SecurityStoreError,
    XorTestProtector,
)


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    def __init__(self, initial=1000.0):
        self.time = initial

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float):
        self.time += seconds


class FakeIdentity:
    def __init__(self, node_id="node-1"):
        self.node_id = node_id
        self.public_key = b"\x00" * 32
        self.private_key = b"\x00" * 32


class _FakeHostRuntime:
    def __init__(self):
        self.started = False

    async def start(self):
        self.started = True
        return self

    async def close(self):
        self.started = False


def _members(count=3):
    return tuple(
        ElectionMember(installation_id=f"inst-{i}", node_id=f"node-{i}")
        for i in range(count)
    )


def test_losing_one_peer_retaining_quorum_does_not_demote(tmp_path):
    """In a 3-member cluster (quorum=2), losing 1 peer leaves self + 1 peer = 2 >= 2."""
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        runtime = _FakeHostRuntime()
        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=runtime,
            credential_store=cred_store,
            clock=clock,
            config=HostAuthorityConfig(lease_duration_seconds=6.0, renewal_interval_seconds=2.0),
        )

        manager.record_initial_lease(1, clock())
        await manager.activate()
        assert manager.role == ACTIVE

        # Quorum is 2. Self is 1. If only node-1 responds (1 peer grant): total = 2.
        # Quorum is satisfied!
        success = manager.renew_lease(grants_count=2)
        assert success is True
        assert manager.role == ACTIVE
        assert manager.condition == "healthy"
        assert manager.consecutive_renewal_failures == 0

        await manager.demote()

    _run(scenario())


def test_repeated_quorum_renewal_failure_triggers_step_down(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        runtime = _FakeHostRuntime()
        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=runtime,
            credential_store=cred_store,
            clock=clock,
            config=HostAuthorityConfig(lease_duration_seconds=6.0, renewal_interval_seconds=2.0, max_renewal_retries=2),
        )

        manager.record_initial_lease(1, clock())
        await manager.activate()
        assert manager.role == ACTIVE

        # Only self responded (1 grant < quorum 2)
        r1 = manager.renew_lease(grants_count=1)
        assert r1 is False
        assert manager.condition == "degraded"
        assert manager.consecutive_renewal_failures == 1
        assert manager.is_authoritative() is True  # Not yet expired

        # Advance past lease expiry
        clock.advance(7.0)
        assert manager.is_authoritative() is False  # Authority expired!

        r2 = manager.renew_lease(grants_count=1)
        assert r2 is False

        await manager.demote(reason="Quorum expired")
        assert manager.role == STANDBY
        assert manager.is_authoritative() is False

    _run(scenario())


def test_quorum_renewal_recovery_before_expiry_retains_authority(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        runtime = _FakeHostRuntime()
        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=runtime,
            credential_store=cred_store,
            clock=clock,
            config=HostAuthorityConfig(lease_duration_seconds=6.0, renewal_interval_seconds=2.0),
        )

        manager.record_initial_lease(1, clock())
        await manager.activate()

        # Transient failure (1 grant < quorum 2)
        clock.advance(2.0)
        r1 = manager.renew_lease(grants_count=1)
        assert r1 is False
        assert manager.condition == "degraded"
        assert manager.consecutive_renewal_failures == 1
        assert manager.is_authoritative() is True

        # Recovery before lease expires (at t=103.0 < 106.0)
        clock.advance(1.0)
        r2 = manager.renew_lease(grants_count=2)
        assert r2 is True
        assert manager.condition == "healthy"
        assert manager.consecutive_renewal_failures == 0
        assert manager.is_authoritative() is True
        assert manager.lease_expires_at == clock() + 6.0

        await manager.demote()

    _run(scenario())


def test_authority_expiration_fences_the_old_host(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        runtime = _FakeHostRuntime()
        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=runtime,
            credential_store=cred_store,
            clock=clock,
            config=HostAuthorityConfig(lease_duration_seconds=5.0),
        )

        manager.record_initial_lease(1, clock())
        await manager.activate()
        assert manager.is_authoritative() is True

        # Clock elapses past lease duration
        clock.advance(5.1)
        assert manager.is_authoritative() is False

        # Node is fenced
        await manager.fence(term=2, reason="Superseded by term 2")
        assert manager.role == FENCED
        assert manager.is_authoritative() is False
        assert manager.condition == "fenced"

    _run(scenario())


def test_new_host_cannot_activate_while_earlier_lease_valid(tmp_path):
    """Voter refuses to vote for Term 2 while lease for Term 1 is still unexpired."""
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))

        sent = []
        async def fake_send(nid, payload):
            sent.append((nid, payload))

        coordinator = ElectionCoordinator("node-1", members, store)
        protocol = ElectionProtocol(
            coordinator,
            store,
            members,
            ElectionConfig(),
            send_to_peer=fake_send,
            get_identity=lambda: FakeIdentity("node-1"),
            get_session_id=lambda nid: f"sess-{nid}",
            get_host_state=lambda: PRE_HOST,
            clock=clock,
            lease_duration=10.0,
            enforce_lease=True,
        )

        from astral_remote.agent.network.election_messages import (
            build_election_request_payload,
            membership_fingerprint,
        )

        fp = membership_fingerprint(members)

        # Node-0 requests vote for Term 1
        msg1 = {
            "version": 1,
            "network": "astral-remote",
            "type": "election-request",
            "nodeId": "node-0",
            "sessionId": "sess-node-0",
            "electionTerm": 1,
            "candidateNodeId": "node-0",
            "membershipFingerprint": fp,
        }
        await protocol.handle_vote_request("node-0", msg1)
        assert sent[-1][1]["voteGranted"] is True
        assert protocol.lease_grant is not None
        assert protocol.lease_grant.term == 1
        assert protocol.lease_grant.is_expired(clock()) is False

        # At t=102.0, Node-2 tries to start election for Term 2
        clock.advance(2.0)
        msg2 = {
            "version": 1,
            "network": "astral-remote",
            "type": "election-request",
            "nodeId": "node-2",
            "sessionId": "sess-node-2",
            "electionTerm": 2,
            "candidateNodeId": "node-2",
            "membershipFingerprint": fp,
        }
        await protocol.handle_vote_request("node-2", msg2)
        # MUST BE REJECTED because term 1 lease is still active!
        assert sent[-1][1]["voteGranted"] is False
        assert "Prior authority lease" in sent[-1][1]["reason"]

        # Advance clock past lease expiration (10.0 seconds from t=100.0 => 110.1)
        clock.advance(9.0)
        assert protocol.lease_grant.is_expired(clock()) is True

        # Now Node-2 retries for Term 2: should be granted!
        await protocol.handle_vote_request("node-2", msg2)
        assert sent[-1][1]["voteGranted"] is True
        assert protocol.lease_grant.term == 2

    _run(scenario())


def test_stale_authority_renewal_and_stale_terms_rejected(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        store.observe_term(3)
        store.grant_vote(3, "node-0")

        sent = []
        async def fake_send(nid, payload):
            sent.append((nid, payload))

        sent = []
        async def fake_send(nid, payload):
            sent.append((nid, payload))

        coordinator = ElectionCoordinator("node-1", members, store)
        protocol = ElectionProtocol(
            coordinator,
            store,
            members,
            ElectionConfig(),
            send_to_peer=fake_send,
            get_identity=lambda: FakeIdentity("node-1"),
            get_session_id=lambda nid: f"sess-{nid}",
            get_host_state=lambda: PRE_HOST,
            clock=clock,
            enforce_lease=True,
        )

        from astral_remote.agent.network.election_messages import (
            membership_fingerprint,
        )
        fp = membership_fingerprint(members)

        # Stale renewal for Term 2 (local is 3)
        msg = {
            "version": 1,
            "network": "astral-remote",
            "type": "lease-renewal-request",
            "nodeId": "node-0",
            "sessionId": "sess-node-0",
            "electionTerm": 2,
            "hostNodeId": "node-0",
            "membershipFingerprint": fp,
        }
        await protocol.handle_lease_renewal_request("node-0", msg)
        assert sent[-1][1]["leaseGranted"] is False
        assert "Stale" in sent[-1][1]["reason"]

    _run(scenario())


def test_two_disjoint_partitions_cannot_independently_hold_quorum(tmp_path):
    """Under static membership N=5, majority quorum is 3.

    Partition A has {node-0, node-1} (size 2 < 3).
    Partition B has {node-2, node-3, node-4} (size 3 >= 3).
    Partition A cannot reach quorum and cannot activate.
    """
    members = _members(5)
    store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
    coordinator = ElectionCoordinator("node-0", members, store)
    assert coordinator.quorum == 3

    coordinator.begin_election()
    # In partition A, only node-1 votes for node-0
    coordinator.record_vote("node-1", 1, True)

    snap = coordinator.snapshot()
    # 2 votes (node-0, node-1) < quorum 3
    assert len(snap.votes_received) == 2
    assert snap.phase != ELECTION_ELECTED


def test_persistence_failure_cannot_grant_or_renew_authority(tmp_path, monkeypatch):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))

        sent = []
        async def fake_send(nid, payload):
            sent.append((nid, payload))

        sent = []
        async def fake_send(nid, payload):
            sent.append((nid, payload))

        coordinator = ElectionCoordinator("node-1", members, store)
        protocol = ElectionProtocol(
            coordinator,
            store,
            members,
            ElectionConfig(),
            send_to_peer=fake_send,
            get_identity=lambda: FakeIdentity("node-1"),
            get_session_id=lambda nid: f"sess-{nid}",
            get_host_state=lambda: PRE_HOST,
            clock=clock,
            enforce_lease=True,
        )

        from astral_remote.agent.network.election_messages import (
            membership_fingerprint,
        )
        fp = membership_fingerprint(members)

        # Force storage error
        def fail_grant(term, candidate):
            raise SecurityStoreError("Disk I/O failure")

        monkeypatch.setattr(store, "grant_vote", fail_grant)

        msg = {
            "version": 1,
            "network": "astral-remote",
            "type": "election-request",
            "nodeId": "node-0",
            "sessionId": "sess-node-0",
            "electionTerm": 1,
            "candidateNodeId": "node-0",
            "membershipFingerprint": fp,
        }
        await protocol.handle_vote_request("node-0", msg)
        assert sent[-1][1]["voteGranted"] is False
        assert "persistence failed" in sent[-1][1]["reason"].lower()

    _run(scenario())


def test_restart_behavior_cannot_inadvertently_overlap_host_authority(tmp_path):
    """A restarted node with a recorded vote enforces reboot cooldown before voting for higher terms."""
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        # Pre-populate store as if voted before crash
        store.observe_term(1)
        store.grant_vote(1, "node-0")

        sent = []
        async def fake_send(nid, payload):
            sent.append((nid, payload))

        coordinator = ElectionCoordinator("node-1", members, store)
        # Node restarts: reboot cooldown is 10.0 seconds
        protocol = ElectionProtocol(
            coordinator,
            store,
            members,
            ElectionConfig(),
            send_to_peer=fake_send,
            get_identity=lambda: FakeIdentity("node-1"),
            get_session_id=lambda nid: f"sess-{nid}",
            get_host_state=lambda: PRE_HOST,
            clock=clock,
            reboot_cooldown=10.0,
            enforce_lease=True,
        )

        from astral_remote.agent.network.election_messages import (
            membership_fingerprint,
        )
        fp = membership_fingerprint(members)

        # Immediately at t=101.0 (1s after reboot), candidate node-2 requests vote for Term 2
        clock.advance(1.0)
        msg = {
            "version": 1,
            "network": "astral-remote",
            "type": "election-request",
            "nodeId": "node-2",
            "sessionId": "sess-node-2",
            "electionTerm": 2,
            "candidateNodeId": "node-2",
            "membershipFingerprint": fp,
        }
        await protocol.handle_vote_request("node-2", msg)
        # Must be rejected because within reboot cooldown!
        assert sent[-1][1]["voteGranted"] is False
        assert "reboot lease cooldown" in sent[-1][1]["reason"]

        # Advance past reboot cooldown (10.0s)
        clock.advance(10.0)
        await protocol.handle_vote_request("node-2", msg)
        # Now allowed!
        assert sent[-1][1]["voteGranted"] is True

    _run(scenario())
