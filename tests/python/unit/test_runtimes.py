"""Unit tests for separate local Agent and Discord Host runtimes."""

import asyncio

import pytest

from astral_remote.agent.runtime import AgentRuntime
from astral_remote.host.runtime import HostRuntime
from astral_remote.shared.security import CredentialStore, SecurityStoreError, XorTestProtector


def _run(coro):
    return asyncio.run(coro)


def test_agent_runtime_starts_and_stops_without_discord_client(tmp_path):
    runtime = AgentRuntime(
        {"agentVersion": "0.1.0"},
        state_path=tmp_path / "identity.json",
        hostname_provider=lambda: "LOCAL-PC",
        uptime_provider=lambda: 42,
    )

    state = _run(runtime.start())

    assert runtime.started is True
    assert state.hostname == "LOCAL-PC"
    assert state.uptime_seconds == 42
    assert state.installation_id
    assert "discord" not in type(runtime).__module__.lower()
    _run(runtime.close())
    assert runtime.started is False


def test_agent_runtime_exposes_local_inventory_through_its_state_interface(tmp_path):
    received = []

    def collect(facts):
        received.append(facts)
        return {"identity": facts}

    async def scenario():
        runtime = AgentRuntime(
            {"agentVersion": "0.1.0"}, state_path=tmp_path / "identity.json",
            hostname_provider=lambda: "LOCAL-PC", uptime_provider=lambda: 42,
            inventory_collector=collect,
        )
        await runtime.start()
        inventory = await runtime.collect_inventory()
        assert inventory["identity"]["hostname"] == "LOCAL-PC"
        assert inventory["identity"]["connected"] is None
        assert received == [runtime.snapshot()]
        await runtime.close()

    _run(scenario())


def test_host_runtime_explicit_start_is_idempotent_and_keeps_agent_running(tmp_path):
    class Client:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

        def is_closed(self):
            return self.closed

    async def scenario():
        agent = AgentRuntime(
            {"agentVersion": "0.1.0"}, state_path=tmp_path / "identity.json",
            hostname_provider=lambda: "LOCAL-PC", uptime_provider=lambda: 1,
        )
        created = []

        async def start_host(config, token):
            created.append((config, token))
            return Client()

        host = HostRuntime(agent, "test-token", {"agentVersion": "0.1.0"}, start_host=start_host)
        first = await host.start()
        second = await host.start()

        assert first is second
        assert len(created) == 1
        assert agent.started is True
        await host.close()
        assert first.closed is True
        assert agent.started is True
        await agent.close()

    _run(scenario())


def test_host_start_failure_does_not_stop_agent_runtime(tmp_path):
    async def scenario():
        class LoginFailure(RuntimeError):
            pass

        agent = AgentRuntime(
            {"agentVersion": "0.1.0"}, state_path=tmp_path / "identity.json",
            hostname_provider=lambda: "LOCAL-PC", uptime_provider=lambda: 1,
        )

        async def fail_start(config, token):
            raise LoginFailure("connection failed")

        host = HostRuntime(agent, "test-token", {"agentVersion": "0.1.0"}, start_host=fail_start)
        with pytest.raises(RuntimeError, match="Discord rejected"):
            await host.start()
        assert agent.started is True
        assert host.started is False
        await agent.close()

    _run(scenario())


def test_missing_credential_refuses_host_without_stopping_agent(tmp_path):
    async def scenario():
        agent = AgentRuntime({"agentVersion": "0.1.0"}, state_path=tmp_path / "identity.json")
        store = CredentialStore(tmp_path / "host.bin", XorTestProtector(b"test"))
        host = HostRuntime(agent, config={"agentVersion": "0.1.0"}, credential_store=store)
        with pytest.raises(SecurityStoreError, match="unavailable"):
            await host.start()
        assert agent.started is True
        assert host.started is False
        await agent.close()

    _run(scenario())
