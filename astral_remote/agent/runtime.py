"""Discord-free runtime for one local Astral Remote Agent installation.

This runtime owns local identity and state collection only.  It deliberately
does not import Discord modules or create network clients; a HostRuntime may
consume its state later when this PC is selected as the server host.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from astral_remote import AGENT_VERSION
from astral_remote.agent.identity import StateStore
from astral_remote.shared.security import (
    IdentityStore,
    NodeIdentity,
    TrustStore
)


@dataclass(frozen=True)
class AgentState:
    """Read-only local state published by an active Agent runtime."""

    hostname: str
    installation_id: str
    agent_version: str
    uptime_seconds: int | None
    node_id: str | None = None

    def facts(self) -> dict:
        """Return report-compatible local facts without a gateway claim."""
        return {
            "hostname": self.hostname,
            "installationId": self.installation_id,
            "agentVersion": self.agent_version,
            "uptimeSeconds": self.uptime_seconds,
            "connected": None,
        }


class AgentRuntime:
    """Manage local Agent state without starting a Discord connection."""

    def __init__(self, config: dict | None = None, *, state_path=None, env=None,
                 store: StateStore | None = None, hostname_provider: Callable | None = None,
                 uptime_provider: Callable | None = None,
                 inventory_collector: Callable | None = None, node_identity_store: IdentityStore | None = None,
                 peer_network=None, peer_network_factory: Callable | None = None):
        self._config = dict(config or {})
        self._store = store if store is not None else StateStore(state_path, env)
        self._hostname_provider = hostname_provider
        self._uptime_provider = uptime_provider
        self._inventory_collector = inventory_collector
        self._node_identity_store = node_identity_store
        self._node_identity: NodeIdentity | None = None
        self._peer_network = peer_network
        self._peer_network_factory = peer_network_factory
        self._state: AgentState | None = None

    @property
    def started(self) -> bool:
        return self._state is not None

    @property
    def state(self) -> AgentState:
        if self._state is None:
            raise RuntimeError("Agent runtime is not started.")
        return self._state

    @property
    def node_identity(self) -> NodeIdentity | None:
        """Cryptographic identity for future peer transport; never report private key."""
        return self._node_identity

    @property
    def peer_network(self):
        """Agent-owned peer transport, or None when disabled/unconfigured."""
        return self._peer_network

    def network_status(self) -> dict:
        return self._peer_network.status() if self._peer_network is not None else {"enabled": False, "listening": False, "peers": []}

    async def start(self) -> AgentState:
        """Initialize local identity and expose state; never opens a gateway."""
        if self._state is not None:
            return self._state
        hostname = self._hostname()
        uptime = self._uptime()
        version = self._config.get("agentVersion") or AGENT_VERSION
        if self._node_identity_store is not None:
            self._node_identity = self._node_identity_store.load_or_create()
        self._state = AgentState(
            hostname=hostname,
            installation_id=self._store.installation_id,
            agent_version=str(version),
            uptime_seconds=uptime,
        )
        await self._start_network_if_enabled()
        return self._state

    async def close(self) -> None:
        """Release runtime-owned in-memory state without affecting a Host."""
        if self._peer_network is not None:
            await self._peer_network.close()
        self._state = None
        self._node_identity = None

    async def _start_network_if_enabled(self) -> None:
        from astral_remote.agent.peer_network import PeerNetwork, parse_network_config
        enabled, _, _, _ = parse_network_config(self._config)
        if not enabled:
            return
        if self._node_identity is None:
            raise RuntimeError("Peer network needs a Node Identity store.")
        if self._peer_network is None:
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                raise RuntimeError("Peer network trust directory is unavailable.")
            factory = self._peer_network_factory or PeerNetwork
            self._peer_network = factory(
                self._node_identity,
                TrustStore(Path(base) / "AstralRemote" / "peer-trust.json"),
                self._config,
                installation_id=self._store.installation_id,
            )
        try:
            await self._peer_network.start()
        except Exception:
            # Local Agent state remains valid when optional peer transport fails.
            await self._peer_network.close()

    def snapshot(self) -> dict:
        """Return local facts for future reporting or peer transport layers."""
        return self.state.facts()

    async def collect_inventory(self) -> dict:
        """Collect local report data without coupling it to any Host gateway.

        The existing collector may block while it samples the machine, so it
        runs in a worker thread just as the legacy Discord publisher did.
        """
        collector = self._inventory_collector
        if collector is None:
            from astral_remote.agent.windows.inventory import collect

            collector = collect
        return await asyncio.to_thread(collector, self.snapshot())

    def _hostname(self) -> str:
        if self._hostname_provider is not None:
            value = self._hostname_provider()
        else:
            from astral_remote.agent.windows.info import get_hostname

            value = get_hostname()
        text = str(value or "").strip()
        return text or "unknown-host"

    def _uptime(self) -> int | None:
        try:
            if self._uptime_provider is not None:
                value = self._uptime_provider()
            else:
                from astral_remote.agent.windows.info import get_uptime_seconds

                value = get_uptime_seconds()
            return int(value) if value is not None and int(value) >= 0 else None
        except (TypeError, ValueError, OSError):
            return None
