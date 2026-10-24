"""Unit tests for Phase 07 Host command authorization and authority gating."""

import asyncio
from typing import Any
import pytest

from astral_remote.agent.network.election import (
    ELECTION_ELECTED,
    ElectionCoordinator,
)
from astral_remote.agent.network.election_messages import (
    build_agent_command_payload,
)
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


def test_authorized_current_term_host_commands_are_accepted(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        ident = _identity(tmp_path, "agent.bin")
        trust_store = TrustStore(tmp_path / "trust.json")
        election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
        election_store.observe_term(1)

        members = (
            ElectionMember("inst-agent", ident.node_id),
            ElectionMember("inst-host", "host-node"),
            ElectionMember("inst-peer", "peer-node"),
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

        agent_runtime = AgentRuntime(
            {"agentVersion": "0.1.0"},
            state_path=tmp_path / "agent-state.json",
            hostname_provider=lambda: "AGENT-PC",
        )
        await agent_runtime.start()

        wall_clock = _Clock(2000.0)
        network = PeerNetwork(
            ident,
            trust_store,
            config,
            installation_id="inst-agent",
            election_state_store=election_store,
            agent_runtime=agent_runtime,
            clock=clock,
            wall_clock=wall_clock,
        )

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

        # Host sends authorized command (leaseExpiresAt well in future)
        msg = {
            "version": 1,
            "network": "astral-remote",
            "type": "agent-command",
            "nodeId": "host-node",
            "hostNodeId": "host-node",
            "targetNodeId": ident.node_id,
            "hostTerm": 1,
            "command": "status",
            "commandId": "cmd-001",
            "leaseExpiresAt": wall_clock.time + 60.0,
        }

        await network._handle_agent_command(session, msg)

        # Reply was sent
        assert len(writer.sent) > 0
        from astral_remote.agent.network.codec import _receive
        import io
        stream = io.BytesIO(b"".join(writer.sent))
        # Framed json was received
        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(writer.sent))
        reader.feed_eof()
        from astral_remote.agent.network.codec import _receive
        reply = await _receive(reader)
        assert reply["type"] == "agent-command-reply"
        assert reply["commandId"] == "cmd-001"
        assert reply["success"] is True
        assert reply["result"]["hostname"] == "AGENT-PC"

        await agent_runtime.close()
        await network.close()

    _run(scenario())


def test_stale_host_term_commands_are_rejected(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        ident = _identity(tmp_path, "agent.bin")
        trust_store = TrustStore(tmp_path / "trust.json")
        election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
        # Local state is already at term 3
        election_store.observe_term(3)

        members = (
            ElectionMember("inst-agent", ident.node_id),
            ElectionMember("inst-host", "host-node"),
            ElectionMember("inst-peer", "peer-node"),
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

        wall_clock2 = _Clock(2000.0)
        network = PeerNetwork(
            ident,
            trust_store,
            config,
            installation_id="inst-agent",
            election_state_store=election_store,
            clock=clock,
            wall_clock=wall_clock2,
        )

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

        # Stale command with term 1 (local is 3); leaseExpiresAt present but term check fires first
        msg = {
            "version": 1,
            "network": "astral-remote",
            "type": "agent-command",
            "nodeId": "host-node",
            "hostNodeId": "host-node",
            "targetNodeId": ident.node_id,
            "hostTerm": 1,
            "command": "status",
            "commandId": "cmd-stale",
            "leaseExpiresAt": wall_clock2.time + 60.0,
        }

        await network._handle_agent_command(session, msg)

        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(writer.sent))
        reader.feed_eof()
        from astral_remote.agent.network.codec import _receive
        reply = await _receive(reader)

        assert reply["type"] == "agent-command-reply"
        assert reply["commandId"] == "cmd-stale"
        assert reply["success"] is False
        assert "Stale" in reply["error"]

        await network.close()

    _run(scenario())


def test_payload_claiming_another_host_node_id_is_rejected(tmp_path):
    async def scenario():
        clock = _Clock(100.0)
        ident = _identity(tmp_path, "agent.bin")
        trust_store = TrustStore(tmp_path / "trust.json")
        election_store = ElectionStateStore(tmp_path / "election.bin", XorTestProtector(b"k"))
        election_store.observe_term(1)

        members = (
            ElectionMember("inst-agent", ident.node_id),
            ElectionMember("inst-host", "host-node"),
            ElectionMember("inst-peer", "peer-node"),
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

        wall_clock3 = _Clock(2000.0)
        network = PeerNetwork(
            ident,
            trust_store,
            config,
            installation_id="inst-agent",
            election_state_store=election_store,
            clock=clock,
            wall_clock=wall_clock3,
        )

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

        # Host-node sends command claiming to be someone-else; hostNodeId mismatch fires before expiry
        msg = {
            "version": 1,
            "network": "astral-remote",
            "type": "agent-command",
            "nodeId": "host-node",
            "hostNodeId": "impostor-node",
            "targetNodeId": ident.node_id,
            "hostTerm": 1,
            "command": "status",
            "commandId": "cmd-spoofed",
            "leaseExpiresAt": wall_clock3.time + 60.0,
        }

        await network._handle_agent_command(session, msg)

        reader = asyncio.StreamReader()
        reader.feed_data(b"".join(writer.sent))
        reader.feed_eof()
        from astral_remote.agent.network.codec import _receive
        reply = await _receive(reader)

        assert reply["type"] == "agent-command-reply"
        assert reply["success"] is False
        assert "Host identity" in reply["error"]

        await network.close()

    _run(scenario())


def test_losing_authority_prevents_new_host_operations(tmp_path):
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

        network = PeerNetwork(
            ident,
            trust_store,
            config,
            installation_id="inst-host",
            election_state_store=election_store,
            clock=clock,
        )

        assert network.is_host_authoritative() is False

        # Attempting to issue a command when not authoritative raises PeerProtocolError
        with pytest.raises(PeerProtocolError, match="valid Host authority"):
            await network.send_agent_command("peer-node", "status")

        # Now simulate becoming active host
        coordinator = network._election_protocol.coordinator
        coordinator.begin_election()
        coordinator.record_vote("peer-node", 1, True)
        assert coordinator.snapshot().phase == ELECTION_ELECTED

        cred_store = CredentialStore(tmp_path / "cred.bin", XorTestProtector(b"k"))
        cred_store.save_token("test-bot-token")
        network._host_lifecycle._credential_store = cred_store

        network._host_lifecycle.record_initial_lease(1, clock())
        await network._host_lifecycle.activate()
        assert network.is_host_authoritative() is True

        # Demote
        await network._host_lifecycle.demote()
        assert network.is_host_authoritative() is False

        # Attempting to issue command after demotion raises PeerProtocolError
        with pytest.raises(PeerProtocolError, match="valid Host authority"):
            await network.send_agent_command("peer-node", "status")

        await network.close()

    _run(scenario())
