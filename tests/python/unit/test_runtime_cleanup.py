"""Unit tests for Phase 07 runtime cleanup, task cancellation, and process failure detection."""

import asyncio
import time
from typing import Any
import pytest

from astral_remote.agent.network.election import (
    ElectionCoordinator,
)
from astral_remote.agent.network.host_presence import (
    FOLLOWING_HOST,
    HOST_UNKNOWN,
    PRE_HOST,
    HostPresenceConfig,
    HostPresenceTracker,
)
from astral_remote.agent.network.host_role import (
    ACTIVE,
    STANDBY,
    HostAuthorityConfig,
    HostLifecycleManager,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.agent.peer_network import PeerNetwork, PeerSession
from astral_remote.agent.runtime import AgentRuntime
from astral_remote.host.runtime import HostRuntime
from astral_remote.shared.security import (
    CredentialStore,
    ElectionStateStore,
    IdentityStore,
    TrustStore,
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


class _FakeWriter:
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


def _identity(tmp_path, name="id.bin"):
    return IdentityStore(tmp_path / name, XorTestProtector(b"k")).load_or_create()


def _members(count=3):
    return tuple(
        ElectionMember(installation_id=f"inst-{i}", node_id=f"node-{i}")
        for i in range(count)
    )


def test_discord_client_shutdown_only_when_host_runtime_owns_client(tmp_path):
    async def scenario():
        agent = AgentRuntime(
            {"agentVersion": "0.1.0"},
            state_path=tmp_path / "agent.json",
        )
        await agent.start()

        class ExternalClient:
            def __init__(self):
                self.closed = False
            async def close(self):
                self.closed = True

        ext_client = ExternalClient()
        runtime = HostRuntime(agent, "token")

        # When client is None (not started / not owned)
        await runtime.close()
        assert ext_client.closed is False

        # When client was started by runtime
        runtime._client = ext_client
        await runtime.close()
        assert ext_client.closed is True
        assert runtime.client is None

        await agent.close()

    _run(scenario())


def test_background_tasks_are_cancelled_and_awaited(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)
        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        class MockRuntime:
            async def start(self):
                return self
            async def close(self):
                pass

        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=MockRuntime(),
            credential_store=cred_store,
            clock=clock,
            config=HostAuthorityConfig(renewal_interval_seconds=0.1),
        )

        manager.record_initial_lease(1, clock())
        await manager.activate()
        assert manager.role == ACTIVE
        assert manager._monitor_task is not None
        assert not manager._monitor_task.done()

        # Demote must cancel and clean up background task
        await manager.demote()
        assert manager.role == STANDBY
        assert manager._monitor_task is None

    _run(scenario())


def test_forced_host_process_termination_detected_by_peers(tmp_path):
    """When a remote Host terminates abruptly, peer's HostPresenceTracker observes failures and reaches PRE_HOST."""
    tracker = HostPresenceTracker(
        HostPresenceConfig(
            heartbeat_interval_seconds=2.0,
            response_timeout_seconds=1.0,
            failure_threshold=3,
            recovery_threshold=1,
        )
    )

    tracker.observe_ack("host-node")
    assert tracker.state == FOLLOWING_HOST

    # Simulated missing heartbeat ACKs due to crashed host
    s1 = tracker.observe_failure()
    assert s1 != PRE_HOST
    s2 = tracker.observe_failure()
    assert s2 != PRE_HOST
    s3 = tracker.observe_failure()
    assert s3 == PRE_HOST
    assert tracker.state == PRE_HOST


def test_election_and_peer_networking_remain_operational_after_demotion(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
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
                    ]
                }
            }
        }

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        network = PeerNetwork(
            ident,
            trust_store,
            config,
            installation_id="inst-host",
            election_state_store=election_store,
            credential_store=cred_store,
            clock=clock,
        )

        coordinator = network.election_protocol.coordinator
        coordinator.begin_election()
        coordinator.record_vote("peer-node", 1, True)

        network.host_lifecycle.record_initial_lease(1, clock())
        await network.host_lifecycle.activate()
        assert network.host_role == ACTIVE

        # Demote the host
        await network.host_lifecycle.demote(reason="Testing demotion")
        assert network.host_role == STANDBY

        # Peer network status is still fully operational
        status = network.status()
        assert status["enabled"] is True
        assert status["hostRole"]["role"] == STANDBY

        # Election protocol can still participate / receive votes for new terms
        from astral_remote.agent.network.election_messages import membership_fingerprint
        fp = membership_fingerprint(members)
        req = {
            "version": 1,
            "network": "astral-remote",
            "type": "election-request",
            "nodeId": "peer-node",
            "sessionId": "sess-peer",
            "electionTerm": 2,
            "candidateNodeId": "peer-node",
            "membershipFingerprint": fp,
        }

        # Advance clock so lease for term 1 expired
        clock.advance(10.0)
        await network.election_protocol.handle_vote_request("peer-node", req)
        assert election_store.load().current_term == 2
        assert election_store.load().voted_for == "peer-node"

        await network.close()

    _run(scenario())
