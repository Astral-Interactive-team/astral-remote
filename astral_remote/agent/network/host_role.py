"""Host role lifecycle, authority lease management, and fencing (Phase 07).

This module defines the explicit Host-role lifecycle for an Astral Remote node:
  - STANDBY: no active Host authority.
  - ACTIVATING: an elected candidate is preparing to acquire authority.
  - ACTIVE: this node holds valid, unexpired Host authority.
  - STEPPING_DOWN: this node is relinquishing authority and stopping Host-only tasks.
  - FENCED: this node is no longer authorized to perform Host-only operations.

This lifecycle is strictly separate from HostPresenceTracker (which observes
remote Host liveness).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from astral_remote.agent.network.election import ELECTION_ELECTED, ElectionCoordinator
from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.logging_utils import error, info, warn
from astral_remote.shared.security import ElectionStateStore, SecurityStoreError

# Host role lifecycle states
STANDBY = "STANDBY"
ACTIVATING = "ACTIVATING"
ACTIVE = "ACTIVE"
STEPPING_DOWN = "STEPPING_DOWN"
FENCED = "FENCED"

HOST_ROLES = (STANDBY, ACTIVATING, ACTIVE, STEPPING_DOWN, FENCED)

# Defaults for lease and authority management
DEFAULT_LEASE_DURATION_SECONDS = 6.0
DEFAULT_RENEWAL_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_RENEWAL_RETRIES = 3
DEFAULT_REBOOT_COOLDOWN_SECONDS = 6.0


@dataclass(frozen=True)
class HostAuthorityConfig:
    """Tunable policy for Host authority leases and quorum renewals."""

    lease_duration_seconds: float = DEFAULT_LEASE_DURATION_SECONDS
    renewal_interval_seconds: float = DEFAULT_RENEWAL_INTERVAL_SECONDS
    max_renewal_retries: int = DEFAULT_MAX_RENEWAL_RETRIES
    reboot_cooldown_seconds: float = DEFAULT_REBOOT_COOLDOWN_SECONDS

    def validated(self) -> "HostAuthorityConfig":
        if (
            isinstance(self.lease_duration_seconds, bool)
            or not isinstance(self.lease_duration_seconds, (int, float))
            or self.lease_duration_seconds <= 0
        ):
            raise ValueError("Lease duration must be a positive number.")
        if (
            isinstance(self.renewal_interval_seconds, bool)
            or not isinstance(self.renewal_interval_seconds, (int, float))
            or self.renewal_interval_seconds <= 0
        ):
            raise ValueError("Renewal interval must be a positive number.")
        if (
            isinstance(self.max_renewal_retries, bool)
            or not isinstance(self.max_renewal_retries, int)
            or self.max_renewal_retries < 0
        ):
            raise ValueError("Max renewal retries must be a non-negative integer.")
        if (
            isinstance(self.reboot_cooldown_seconds, bool)
            or not isinstance(self.reboot_cooldown_seconds, (int, float))
            or self.reboot_cooldown_seconds < 0
        ):
            raise ValueError("Reboot cooldown must be a non-negative number.")
        return self


def parse_host_authority_config(config: dict[str, Any] | None) -> HostAuthorityConfig:
    """Parse host authority and lease settings from peerNetwork configuration."""
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Peer network configuration is invalid.")

    # Check peerNetwork.hostAuthority, then peerNetwork.election
    section = raw.get("hostAuthority")
    if section is None:
        section = raw.get("election", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("Host authority configuration is invalid.")

    return HostAuthorityConfig(
        lease_duration_seconds=section.get(
            "leaseDurationSeconds", DEFAULT_LEASE_DURATION_SECONDS
        ),
        renewal_interval_seconds=section.get(
            "renewalIntervalSeconds", DEFAULT_RENEWAL_INTERVAL_SECONDS
        ),
        max_renewal_retries=section.get(
            "maxRenewalRetries", DEFAULT_MAX_RENEWAL_RETRIES
        ),
        reboot_cooldown_seconds=section.get(
            "rebootCooldownSeconds", DEFAULT_REBOOT_COOLDOWN_SECONDS
        ),
    ).validated()


@dataclass(frozen=True)
class LeaseGrant:
    """Evidence of authority lease granted by a voter to a candidate term."""

    term: int
    candidate_node_id: str
    granted_at: float
    duration: float

    def is_expired(self, now: float) -> bool:
        return now >= (self.granted_at + self.duration)

    @property
    def expires_at(self) -> float:
        return self.granted_at + self.duration


@dataclass(frozen=True)
class HostRoleStatus:
    """Diagnostic snapshot of local Host authority and role state."""

    role: str
    term: int
    is_authoritative: bool
    lease_expires_at: float | None
    lease_remaining_seconds: float | None
    consecutive_renewal_failures: int
    condition: str
    transition_reason: str | None = None


class HostLifecycleManager:
    """Manage the local Host authority lifecycle, lease renewals, and fencing."""

    def __init__(
        self,
        local_node_id: str,
        local_installation_id: str,
        members: tuple[ElectionMember, ...],
        state_store: ElectionStateStore,
        *,
        coordinator: ElectionCoordinator | None = None,
        config: HostAuthorityConfig | None = None,
        host_runtime: Any = None,
        credential_store: Any = None,
        token: str | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        broadcast_renewal: Callable[[int], Awaitable[int]] | None = None,
    ) -> None:
        if (
            not isinstance(local_node_id, str)
            or not local_node_id.strip()
            or local_node_id != local_node_id.strip()
        ):
            raise ValueError("Local node ID is invalid.")
        if (
            not isinstance(local_installation_id, str)
            or not local_installation_id.strip()
            or local_installation_id != local_installation_id.strip()
        ):
            raise ValueError("Local installation ID is invalid.")
        if not members:
            raise ValueError("Election membership must not be empty.")

        member_map = {m.node_id: m for m in members}
        if local_node_id not in member_map:
            raise ValueError("Local node is not an election member.")
        if member_map[local_node_id].installation_id != local_installation_id:
            raise ValueError("Local installation ID does not match membership.")

        self._local_node_id = local_node_id
        self._local_installation_id = local_installation_id
        self._members = members
        self._member_node_ids = frozenset(member_map.keys())
        self._state_store = state_store
        self._coordinator = coordinator
        self._config = (config or HostAuthorityConfig()).validated()
        self._host_runtime = host_runtime
        self._credential_store = credential_store
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._clock = clock or time.monotonic
        self._broadcast_renewal = broadcast_renewal

        self._quorum = len(members) // 2 + 1
        self._role = STANDBY
        self._term = 0
        self._lease_expires_at = 0.0
        self._consecutive_renewal_failures = 0
        self._condition = "inactive"
        self._transition_reason: str | None = "Initialized"

        self._lock: asyncio.Lock | None = None
        self._monitor_task: asyncio.Task | None = None

        # Paired wall-clock (time.time()) lease expiry maintained alongside the
        # monotonic _lease_expires_at.  Used only for the leaseExpiresAt field
        # in agent-command messages; all local authority decisions still use the
        # monotonic clock so they are immune to NTP adjustments.
        import time as _time
        self._wall_clock: Callable[[], float] = wall_clock if wall_clock is not None else _time.time
        self._wall_clock_lease_expires_at: float = 0.0

    @property
    def role(self) -> str:
        return self._role

    @property
    def term(self) -> int:
        return self._term

    @property
    def quorum(self) -> int:
        return self._quorum

    @property
    def condition(self) -> str:
        return self._condition

    @property
    def consecutive_renewal_failures(self) -> int:
        return self._consecutive_renewal_failures

    @property
    def lease_expires_at(self) -> float:
        return self._lease_expires_at

    @property
    def wall_clock_lease_expires_at(self) -> float:
        """Absolute wall-clock Unix timestamp (time.time()) when the lease expires.

        Returns 0.0 if no lease has been recorded.  Intended only for embedding
        in agent-command payloads so remote Agents can validate expiry against
        their own wall clock.  Do NOT use for local authority decisions; always
        use lease_expires_at with the monotonic clock for those.
        """
        return self._wall_clock_lease_expires_at

    @property
    def host_runtime(self) -> Any:
        return self._host_runtime

    def set_host_runtime(self, runtime: Any) -> None:
        self._host_runtime = runtime

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def is_authoritative(self) -> bool:
        """True if and only if this node holds valid, unexpired Host authority."""
        if self._role != ACTIVE:
            return False
        if self._clock() >= self._lease_expires_at:
            return False
        return True

    def status(self) -> HostRoleStatus:
        """Return an immutable snapshot of Host authority state."""
        now = self._clock()
        remaining = max(0.0, self._lease_expires_at - now) if self._lease_expires_at > 0 else None
        return HostRoleStatus(
            role=self._role,
            term=self._term,
            is_authoritative=self.is_authoritative(),
            lease_expires_at=self._lease_expires_at if self._lease_expires_at > 0 else None,
            lease_remaining_seconds=remaining,
            consecutive_renewal_failures=self._consecutive_renewal_failures,
            condition=self._condition,
            transition_reason=self._transition_reason,
        )

    def can_attempt_activation(self) -> tuple[bool, str]:
        """Validate all prerequisite invariants before attempting activation."""
        if self._coordinator is None:
            return False, "Election coordinator is not configured."

        snap = self._coordinator.snapshot()
        if snap.phase != ELECTION_ELECTED:
            return False, f"Node is not elected (phase is {snap.phase})."
        if snap.candidate_node_id != self._local_node_id:
            return False, "Elected candidate is not this node."

        try:
            persisted = self._state_store.load()
        except SecurityStoreError as exc:
            return False, f"Election state storage error: {exc}"

        if snap.term != persisted.current_term:
            return False, "Coordinator term does not match persisted state store."

        # Check membership
        if self._local_node_id not in self._member_node_ids:
            return False, "Local node is not a configured election member."

        # Check authority lease validity
        now = self._clock()
        if self._lease_expires_at <= 0 or now >= self._lease_expires_at:
            return False, "Host authority lease has expired or was not granted."

        # Check credentials
        if not self._check_credentials_available():
            return False, "Host Discord credentials are unavailable."

        return True, "Prerequisites met."

    def _check_credentials_available(self) -> bool:
        """Verify local credentials exist without transmitting or logging them."""
        if self._token:
            return True
        store = self._credential_store
        if store is None:
            from astral_remote.shared.security import HostCredentialStore
            store = HostCredentialStore()
            self._credential_store = store
        try:
            token = store.load_token()
            return bool(isinstance(token, str) and token.strip())
        except Exception:
            return False

    def record_initial_lease(self, term: int, granted_at: float | None = None) -> None:
        """Record the initial authority lease obtained from the election quorum."""
        now = self._clock() if granted_at is None else granted_at
        self._term = term
        self._lease_expires_at = now + self._config.lease_duration_seconds
        # Derive wall-clock expiry from remaining monotonic duration so the two
        # clocks stay consistent without requiring a shared epoch.
        _remaining = self._lease_expires_at - self._clock()
        self._wall_clock_lease_expires_at = self._wall_clock() + _remaining
        self._consecutive_renewal_failures = 0
        info(
            "Initial Host authority lease recorded.",
            {"term": term, "duration": self._config.lease_duration_seconds},
        )

    async def activate(self) -> bool:
        """Attempt to acquire Host authority and start Host Runtime.

        Idempotent: calling when already ACTIVE returns True safely.
        If prerequisites fail or startup errors occur, fails closed to STANDBY.
        """
        lock = self._get_lock()
        async with lock:
            if self._role == ACTIVE:
                return True
            if self._role == ACTIVATING:
                return False

            allowed, reason = self.can_attempt_activation()
            if not allowed:
                warn("Host activation refused.", {"reason": reason})
                return False

            self._transition_to(ACTIVATING, reason=f"Elected term {self._term}")

            try:
                if self._host_runtime is not None:
                    await self._host_runtime.start()
                self._transition_to(ACTIVE, reason="Authority acquired; Host runtime started")
                self._consecutive_renewal_failures = 0
                self._condition = "healthy"
                self._start_quorum_monitor()
                info("Host authority activated.", {"term": self._term})
                return True
            except Exception as exc:
                error("Host activation failed during startup.", {"error": str(exc)})
                if self._host_runtime is not None:
                    try:
                        await self._host_runtime.close()
                    except Exception:
                        pass
                self._transition_to(STANDBY, reason=f"Activation failed: {exc}")
                self._condition = "inactive"
                return False

    def renew_lease(self, grants_count: int) -> bool:
        """Process quorum renewal grants for the active Host term.

        Returns True if quorum was maintained and lease extended.
        """
        if self._role != ACTIVE:
            return False

        now = self._clock()
        if grants_count >= self._quorum:
            if now < self._lease_expires_at:
                self._lease_expires_at = now + self._config.lease_duration_seconds
                # Keep wall-clock expiry in sync with the renewed monotonic expiry.
                _remaining = self._lease_expires_at - self._clock()
                self._wall_clock_lease_expires_at = self._wall_clock() + _remaining
                self._consecutive_renewal_failures = 0
                self._condition = "healthy"
                return True

        # Degraded or renewal failed
        self._consecutive_renewal_failures += 1
        self._condition = "degraded"
        return False

    async def demote(self, reason: str | None = None) -> None:
        """Relinquish Host authority and stop Host-only operations idempotently.

        Preserves Agent Runtime, Node Identity, and peer network.
        """
        lock = self._get_lock()
        async with lock:
            if self._role in (STANDBY, FENCED) and self._monitor_task is None:
                return

            target_role = FENCED if self._role == FENCED else STANDBY
            self._transition_to(STEPPING_DOWN, reason=reason or "Demotion requested")
            self._condition = "inactive"

            self._stop_quorum_monitor()

            if self._host_runtime is not None:
                try:
                    await self._host_runtime.close()
                except Exception as exc:
                    warn("Error closing HostRuntime during demotion.", {"error": str(exc)})

            self._transition_to(target_role, reason=reason or "Demotion complete")
            self._condition = "fenced" if target_role == FENCED else "inactive"
            info("Host authority relinquished.", {"role": self._role, "reason": reason})

    async def fence(self, term: int | None = None, reason: str | None = None) -> None:
        """Fence this node out of Host authority due to a higher term or conflict."""
        fenced_reason = reason or f"Fenced out by higher term {term}"
        self._role = FENCED
        await self.demote(reason=fenced_reason)
        self._role = FENCED
        self._condition = "fenced"
        warn("Node fenced out of Host authority.", {"term": term, "reason": fenced_reason})

    def observe_term(self, higher_term: int) -> None:
        """Observe a higher term from the network and fence this node if active."""
        if type(higher_term) is not int or higher_term <= self._term:
            return
        if self._role in (ACTIVE, ACTIVATING, STEPPING_DOWN):
            # Spawn fence task
            asyncio.create_task(
                self.fence(higher_term, f"Observed higher term {higher_term}")
            )
        else:
            self._term = higher_term

    def _transition_to(self, new_role: str, reason: str | None = None) -> None:
        old_role = self._role
        self._role = new_role
        self._transition_reason = reason
        info(
            "Host role transition.",
            {"from": old_role, "to": new_role, "reason": reason},
        )

    def _start_quorum_monitor(self) -> None:
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._run_quorum_monitor())

    def _stop_quorum_monitor(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _run_quorum_monitor(self) -> None:
        """Periodically renew Host authority with configured quorum."""
        try:
            while self._role == ACTIVE:
                await asyncio.sleep(self._config.renewal_interval_seconds)
                if self._role != ACTIVE:
                    break

                # Count grants: self + responding peers
                grants = 1  # Local node
                if self._broadcast_renewal is not None:
                    try:
                        peer_grants = await self._broadcast_renewal(self._term)
                        grants += peer_grants
                    except Exception as exc:
                        warn("Quorum renewal broadcast failed.", {"error": str(exc)})

                renewed = self.renew_lease(grants)
                now = self._clock()

                if not renewed:
                    warn(
                        "Quorum renewal failed.",
                        {
                            "grants": grants,
                            "quorum": self._quorum,
                            "failures": self._consecutive_renewal_failures,
                            "leaseRemaining": max(0.0, self._lease_expires_at - now),
                        },
                    )
                    if (
                        now >= self._lease_expires_at
                        or self._consecutive_renewal_failures
                        >= self._config.max_renewal_retries
                    ):
                        warn(
                            "Quorum lost and authority expired; stepping down.",
                            {"term": self._term},
                        )
                        await self.demote(reason="Quorum renewal failure")
                        break
        except asyncio.CancelledError:
            pass
