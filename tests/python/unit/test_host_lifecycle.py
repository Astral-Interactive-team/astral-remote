"""Unit tests for Phase 07 Host lifecycle, activation, and demotion."""

import asyncio
from typing import Any
import pytest

from astral_remote.agent.network.election import (
    ELECTION_CANDIDATE,
    ELECTION_ELECTED,
    ELECTION_IDLE,
    ElectionCoordinator,
)
from astral_remote.agent.network.host_role import (
    ACTIVE,
    ACTIVATING,
    FENCED,
    STANDBY,
    STEPPING_DOWN,
    HostAuthorityConfig,
    HostLifecycleManager,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.agent.runtime import AgentRuntime
from astral_remote.host.runtime import HostRuntime
from astral_remote.shared.security import (
    CredentialStore,
    ElectionState,
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


class _FakeHostRuntime:
    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.started = False
        self.start_calls = 0
        self.close_calls = 0

    async def start(self):
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("Discord connection refused")
        self.started = True
        return self

    async def close(self):
        self.close_calls += 1
        self.started = False


def _members(count=3):
    return tuple(
        ElectionMember(installation_id=f"inst-{i}", node_id=f"node-{i}")
        for i in range(count)
    )


def test_successful_activation_after_valid_election(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        # Win election
        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)
        assert coordinator.snapshot().phase == ELECTION_ELECTED

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
        )

        manager.record_initial_lease(1, clock())
        success = await manager.activate()

        assert success is True
        assert manager.role == ACTIVE
        assert manager.is_authoritative() is True
        assert runtime.started is True
        assert runtime.start_calls == 1

        await manager.demote()

    _run(scenario())


def test_activation_rejected_without_election_success(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        # Candidate only, not elected
        coordinator.begin_election()
        assert coordinator.snapshot().phase == ELECTION_CANDIDATE

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
        )

        manager.record_initial_lease(1, clock())
        success = await manager.activate()

        assert success is False
        assert manager.role == STANDBY
        assert manager.is_authoritative() is False
        assert runtime.started is False

    _run(scenario())


def test_activation_rejected_when_credentials_missing(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        # Win election
        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)
        assert coordinator.snapshot().phase == ELECTION_ELECTED

        # Empty credential store
        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))

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
        )

        manager.record_initial_lease(1, clock())
        success = await manager.activate()

        assert success is False
        assert manager.role == STANDBY
        assert manager.is_authoritative() is False
        assert runtime.started is False

    _run(scenario())


def test_repeated_activation_does_not_create_duplicate_clients_or_tasks(tmp_path):
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
        )

        manager.record_initial_lease(1, clock())
        first = await manager.activate()
        second = await manager.activate()

        assert first is True
        assert second is True
        assert runtime.start_calls == 1
        assert manager.role == ACTIVE

        await manager.demote()

    _run(scenario())


def test_demotion_is_idempotent(tmp_path):
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
        )

        manager.record_initial_lease(1, clock())
        await manager.activate()
        assert manager.role == ACTIVE

        await manager.demote()
        assert manager.role == STANDBY
        assert runtime.close_calls == 1

        # Second demotion is safe no-op
        await manager.demote()
        assert manager.role == STANDBY
        assert runtime.close_calls == 1

    _run(scenario())


def test_failed_activation_does_not_leave_partially_started_resources(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        runtime = _FakeHostRuntime(fail_start=True)
        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=runtime,
            credential_store=cred_store,
            clock=clock,
        )

        manager.record_initial_lease(1, clock())
        success = await manager.activate()

        assert success is False
        assert manager.role == STANDBY
        assert manager.is_authoritative() is False
        assert runtime.started is False
        assert runtime.close_calls == 1

    _run(scenario())


def test_agent_runtime_and_peer_network_remain_alive_after_demotion(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        members = _members(3)
        store = ElectionStateStore(tmp_path / "election.json", XorTestProtector(b"k"))
        coordinator = ElectionCoordinator("node-0", members, store)

        coordinator.begin_election()
        coordinator.record_vote("node-1", 1, True)

        agent = AgentRuntime(
            {"agentVersion": "0.1.0"},
            state_path=tmp_path / "identity.json",
            hostname_provider=lambda: "NODE-0",
            uptime_provider=lambda: 100,
        )
        await agent.start()
        assert agent.started is True

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")

        host_runtime = HostRuntime(agent, "test-bot-token", credential_store=cred_store)
        # Mock client on host runtime
        class MockClient:
            def __init__(self):
                self.closed = False
            async def close(self):
                self.closed = True

        mock_client = MockClient()
        host_runtime._client = mock_client

        manager = HostLifecycleManager(
            local_node_id="node-0",
            local_installation_id="inst-0",
            members=members,
            state_store=store,
            coordinator=coordinator,
            host_runtime=host_runtime,
            credential_store=cred_store,
            clock=clock,
        )

        manager.record_initial_lease(1, clock())
        manager._transition_to(ACTIVE, "Manual")

        await manager.demote(reason="Testing demotion")

        assert manager.role == STANDBY
        assert manager.is_authoritative() is False
        assert mock_client.closed is True
        # Agent runtime remains started and operational!
        assert agent.started is True
        assert agent.snapshot()["hostname"] == "NODE-0"

        await agent.close()

    _run(scenario())
