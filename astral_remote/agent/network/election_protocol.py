"""Async election protocol connecting coordinator, peer sessions, and host presence.

This module owns the campaign lifecycle: starting elections when PRE_HOST
is reached, soliciting votes from authenticated peers, counting responses,
and declaring an election result. It does NOT promote the winner to Host;
that belongs to Phase 07.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from astral_remote.agent.network.election import (
    ELECTION_CANDIDATE,
    ELECTION_ELECTED,
    ELECTION_IDLE,
    ElectionCoordinator,
    ElectionSnapshot,
)
from astral_remote.agent.network.election_messages import (
    build_election_request_payload,
    build_election_response_payload,
    build_lease_renewal_request_payload,
    build_lease_renewal_response_payload,
    membership_fingerprint,
)
from astral_remote.agent.network.host_role import (
    DEFAULT_LEASE_DURATION_SECONDS,
    DEFAULT_REBOOT_COOLDOWN_SECONDS,
    LeaseGrant,
)
from astral_remote.agent.network.errors import PeerProtocolError
from astral_remote.agent.network.host_presence import (
    FOLLOWING_HOST,
    HOST_CONFLICT,
    PRE_HOST,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.logging_utils import info, warn, error
from astral_remote.shared.security import (
    ElectionStateStore,
    SecurityStoreError,
)

# Default election timing policy.
DEFAULT_ELECTION_TIMEOUT_SECONDS = 10.0
DEFAULT_ELECTION_RETRY_DELAY_SECONDS = 3.0
DEFAULT_ELECTION_MAX_RETRIES = 3


@dataclass(frozen=True)
class ElectionConfig:
    """Tunable election campaign policy."""

    timeout_seconds: float = DEFAULT_ELECTION_TIMEOUT_SECONDS
    retry_delay_seconds: float = DEFAULT_ELECTION_RETRY_DELAY_SECONDS
    max_retries: int = DEFAULT_ELECTION_MAX_RETRIES

    def validated(self) -> "ElectionConfig":
        if (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or self.timeout_seconds <= 0):
            raise ValueError("Election timeout must be a positive number.")
        if (isinstance(self.retry_delay_seconds, bool)
                or not isinstance(self.retry_delay_seconds, (int, float))
                or self.retry_delay_seconds <= 0):
            raise ValueError("Election retry delay must be a positive number.")
        if (isinstance(self.max_retries, bool)
                or not isinstance(self.max_retries, int)
                or self.max_retries < 0):
            raise ValueError("Election max retries must be a non-negative integer.")
        return self


def parse_election_protocol_config(
    config: dict[str, Any] | None,
) -> ElectionConfig:
    """Parse optional ``peerNetwork.election`` timing policy."""
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Peer network configuration is invalid.")
    section = raw.get("election")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("Election configuration is invalid.")
    return ElectionConfig(
        timeout_seconds=section.get(
            "timeoutSeconds", DEFAULT_ELECTION_TIMEOUT_SECONDS),
        retry_delay_seconds=section.get(
            "retryDelaySeconds", DEFAULT_ELECTION_RETRY_DELAY_SECONDS),
        max_retries=section.get(
            "maxRetries", DEFAULT_ELECTION_MAX_RETRIES),
    ).validated()


class ElectionProtocol:
    """Manage the full election lifecycle over authenticated peer sessions.

    Responsibilities:
      - Receive vote requests and respond safely (voter side).
      - Start campaigns when PRE_HOST is reached (candidate side).
      - Broadcast requests, collect responses, drive ElectionCoordinator.
      - Respect timeouts, retries, and shutdown.

    Does NOT promote the winner to Host (Phase 07).
    """

    def __init__(
        self,
        coordinator: ElectionCoordinator,
        state_store: ElectionStateStore,
        members: tuple[ElectionMember, ...],
        election_config: ElectionConfig,
        *,
        send_to_peer: Callable,   # async (node_id, payload) -> None
        get_identity: Callable,   # () -> NodeIdentity
        get_session_id: Callable, # (node_id) -> str | None
        get_host_state: Callable, # () -> str
        on_elected: Callable[[int], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        lease_duration: float = DEFAULT_LEASE_DURATION_SECONDS,
        reboot_cooldown: float = DEFAULT_REBOOT_COOLDOWN_SECONDS,
        enforce_lease: bool = False,
    ) -> None:
        self._coordinator = coordinator
        self._state_store = state_store
        self._members = members
        self._config = election_config
        self._send_to_peer = send_to_peer
        self._get_identity = get_identity
        self._get_session_id = get_session_id
        self._get_host_state = get_host_state
        self._on_elected = on_elected
        self._clock = clock or time.monotonic
        self._lease_duration = float(lease_duration)
        self._reboot_cooldown = float(reboot_cooldown)
        self._enforce_lease = bool(enforce_lease)
        self._started_at = self._clock()
        self._lease_grant: LeaseGrant | None = None
        self._active_renewal_round: tuple[int, set[str]] | None = None
        self._fingerprint = membership_fingerprint(members)
        self._member_node_ids = frozenset(m.node_id for m in members)
        self._campaign_task: asyncio.Task | None = None
        self._shutdown = False

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def coordinator(self) -> ElectionCoordinator:
        return self._coordinator

    @property
    def lease_grant(self) -> LeaseGrant | None:
        return self._lease_grant

    def snapshot(self) -> ElectionSnapshot:
        return self._coordinator.snapshot()

    # ------------------------------------------------------------------
    # Voter side: handle incoming election-request
    # ------------------------------------------------------------------

    async def handle_vote_request(
        self,
        authenticated_node_id: str,
        message: dict[str, Any],
    ) -> None:
        """Process an election-request from an authenticated peer session.

        The caller has already verified the Ed25519 signature and session
        identity.  This method enforces election-specific safety checks
        and sends a response.
        """
        identity = self._get_identity()
        term = message.get("electionTerm")
        candidate = message.get("candidateNodeId")
        remote_fingerprint = message.get("membershipFingerprint")
        session_id = message.get("sessionId")

        # Identity in message body must match authenticated session identity.
        msg_node_id = message.get("nodeId")
        if msg_node_id != authenticated_node_id:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Message identity does not match session.")
            return

        # Candidate must be the sender (candidates request votes for themselves).
        if candidate != authenticated_node_id:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Candidate does not match sender.")
            return

        # Validate term.
        if not isinstance(term, int) or term < 1:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Invalid election term.")
            return

        # Membership fingerprint must match.
        if remote_fingerprint != self._fingerprint:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Membership fingerprint mismatch.")
            return

        # Candidate must be a configured member.
        if candidate not in self._member_node_ids:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Candidate is not an election member.")
            return

        # Local node must also be a configured member.
        if identity.node_id not in self._member_node_ids:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Local node is not an election member.")
            return

        # Fencing invariant: A single voter cannot grant overlapping Host authority
        # to conflicting terms. Do not grant a vote to a newer term while an earlier
        # authority grant is still valid.
        current = self._state_store.load()
        now = self._clock()
        if self._enforce_lease:
            if (
                self._lease_grant is not None
                and not self._lease_grant.is_expired(now)
                and self._lease_grant.term < term
            ):
                await self._send_vote_response(
                    authenticated_node_id, session_id, term, candidate,
                    False, f"Prior authority lease for term {self._lease_grant.term} is still active.")
                return

            # Fencing invariant: Restart behavior cannot inadvertently overlap two valid Host authorities.
            if (
                current.current_term > 0
                and current.voted_for is not None
                and term > current.current_term
                and (now - self._started_at) < self._reboot_cooldown
            ):
                await self._send_vote_response(
                    authenticated_node_id, session_id, term, candidate,
                    False, "Node is in reboot lease cooldown.")
                return

        # Observe higher terms (clears previous vote and ends any local campaign).
        if term > current.current_term:
            self._state_store.observe_term(term)
        if term > self._coordinator.snapshot().term:
            self._coordinator.record_vote("", term, False)

        # Reject stale terms.
        current = self._state_store.load()
        if term < current.current_term:
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Stale election term.")
            return

        # Attempt to grant the vote — persist before responding.
        try:
            granted = self._state_store.grant_vote(term, candidate)
        except SecurityStoreError as exc:
            # Storage failure → fail closed, no vote granted.
            warn("Election vote persistence failed.", {"error": str(exc)})
            await self._send_vote_response(
                authenticated_node_id, session_id, term, candidate,
                False, "Vote persistence failed.")
            return

        if granted:
            self._lease_grant = LeaseGrant(
                term=term,
                candidate_node_id=candidate,
                granted_at=now,
                duration=self._lease_duration,
            )

        await self._send_vote_response(
            authenticated_node_id, session_id, term, candidate,
            granted,
            None if granted else "Vote already cast for another candidate.")

    async def _send_vote_response(
        self,
        peer_node_id: str,
        session_id: str | None,
        term: int | None,
        candidate: str | None,
        granted: bool,
        reason: str | None,
    ) -> None:
        identity = self._get_identity()
        actual_session_id = self._get_session_id(peer_node_id) or session_id
        if not actual_session_id:
            return  # Peer disconnected, nothing to send.
        try:
            payload = build_election_response_payload(
                identity,
                actual_session_id,
                term if isinstance(term, int) else 0,
                candidate if isinstance(candidate, str) else "",
                granted,
                self._fingerprint,
                reason,
            )
            await self._send_to_peer(peer_node_id, payload)
        except (OSError, PeerProtocolError) as exc:
            warn("Failed to send election response.",
                 {"peer": peer_node_id, "error": str(exc)})

    # ------------------------------------------------------------------
    # Voter side: handle incoming election-response
    # ------------------------------------------------------------------

    async def handle_vote_response(
        self,
        authenticated_node_id: str,
        message: dict[str, Any],
    ) -> None:
        """Process an election-response from an authenticated peer."""
        term = message.get("electionTerm")
        candidate = message.get("candidateNodeId")
        granted = message.get("voteGranted")
        remote_fingerprint = message.get("membershipFingerprint")

        # Identity in message must match session.
        msg_node_id = message.get("nodeId")
        if msg_node_id != authenticated_node_id:
            return

        # Membership must match.
        if remote_fingerprint != self._fingerprint:
            warn("Election response has mismatched membership.",
                 {"peer": authenticated_node_id})
            return

        if not isinstance(term, int) or term < 1:
            return

        # Response must be for our candidacy.
        identity = self._get_identity()
        if candidate != identity.node_id:
            # Vote for another candidate — ignore, never count.
            return

        # Voter must be a member.
        if authenticated_node_id not in self._member_node_ids:
            return

        # Feed into coordinator (handles term/phase/duplicate checks).
        self._coordinator.record_vote(
            authenticated_node_id,
            term,
            granted is True,
        )

        snap = self._coordinator.snapshot()
        if snap.phase == ELECTION_ELECTED:
            info("Election won.",
                 {"term": snap.term, "votes": len(snap.votes_received),
                  "quorum": snap.quorum})
            if self._on_elected is not None:
                asyncio.create_task(self._on_elected(snap.term))

    # ------------------------------------------------------------------
    # Lease renewal handling (Phase 07)
    # ------------------------------------------------------------------

    async def handle_lease_renewal_request(
        self,
        authenticated_node_id: str,
        message: dict[str, Any],
    ) -> None:
        """Process a Host authority lease renewal request from an authenticated peer."""
        term = message.get("electionTerm")
        host_node_id = message.get("hostNodeId")
        remote_fingerprint = message.get("membershipFingerprint")
        session_id = message.get("sessionId")

        if message.get("nodeId") != authenticated_node_id or host_node_id != authenticated_node_id:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Identity does not match session.")
            return

        if not isinstance(term, int) or term < 1:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Invalid election term.")
            return

        if remote_fingerprint != self._fingerprint:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Membership fingerprint mismatch.")
            return

        if host_node_id not in self._member_node_ids:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Host is not an election member.")
            return

        try:
            current = self._state_store.load()
        except SecurityStoreError:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Storage failure.")
            return

        if term < current.current_term:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Stale election term.")
            return

        if term > current.current_term:
            self._state_store.observe_term(term)
            current = self._state_store.load()

        if current.voted_for is not None and current.voted_for != host_node_id:
            await self._send_lease_renewal_response(
                authenticated_node_id, session_id, term, host_node_id,
                False, "Vote cast for another candidate.")
            return

        now = self._clock()
        self._lease_grant = LeaseGrant(
            term=term,
            candidate_node_id=host_node_id,
            granted_at=now,
            duration=self._lease_duration,
        )
        await self._send_lease_renewal_response(
            authenticated_node_id, session_id, term, host_node_id,
            True, None)

    async def _send_lease_renewal_response(
        self,
        peer_node_id: str,
        session_id: str | None,
        term: int | None,
        host_node_id: str | None,
        granted: bool,
        reason: str | None,
    ) -> None:
        identity = self._get_identity()
        actual_session_id = self._get_session_id(peer_node_id) or session_id
        if not actual_session_id:
            return
        try:
            payload = build_lease_renewal_response_payload(
                identity,
                actual_session_id,
                term if isinstance(term, int) else 0,
                host_node_id if isinstance(host_node_id, str) else "",
                granted,
                self._fingerprint,
                reason,
            )
            await self._send_to_peer(peer_node_id, payload)
        except (OSError, PeerProtocolError) as exc:
            warn("Failed to send lease renewal response.",
                 {"peer": peer_node_id, "error": str(exc)})

    async def handle_lease_renewal_response(
        self,
        authenticated_node_id: str,
        message: dict[str, Any],
    ) -> None:
        """Process a Host authority lease renewal response."""
        term = message.get("electionTerm")
        host_node_id = message.get("hostNodeId")
        granted = message.get("leaseGranted")
        remote_fingerprint = message.get("membershipFingerprint")

        if message.get("nodeId") != authenticated_node_id:
            return
        if remote_fingerprint != self._fingerprint:
            return
        if not isinstance(term, int) or term < 1:
            return
        identity = self._get_identity()
        if host_node_id != identity.node_id:
            return

        if self._active_renewal_round is not None:
            round_term, round_responses = self._active_renewal_round
            if term == round_term and granted is True:
                round_responses.add(authenticated_node_id)

    async def broadcast_lease_renewal(self, term: int, timeout_seconds: float = 1.0) -> int:
        """Broadcast lease renewal request to members and return count of granted responses."""
        identity = self._get_identity()
        responses: set[str] = set()
        self._active_renewal_round = (term, responses)
        try:
            for node_id in self._member_node_ids:
                if node_id == identity.node_id:
                    continue
                session_id = self._get_session_id(node_id)
                if session_id is None:
                    continue
                try:
                    payload = build_lease_renewal_request_payload(
                        identity,
                        session_id,
                        term,
                        identity.node_id,
                        self._fingerprint,
                    )
                    await self._send_to_peer(node_id, payload)
                except (OSError, PeerProtocolError):
                    pass

            start_wait = self._clock()
            needed_peer_grants = (len(self._members) // 2 + 1) - 1
            while len(responses) < needed_peer_grants:
                if (self._clock() - start_wait) >= timeout_seconds:
                    break
                await asyncio.sleep(0.05)
            return len(responses)
        finally:
            self._active_renewal_round = None

    # ------------------------------------------------------------------
    # Candidate side: start and run campaigns
    # ------------------------------------------------------------------

    def should_start_election(self) -> bool:
        """Check if conditions permit starting an election."""
        if self._shutdown:
            return False
        if not self._members:
            return False
        host_state = self._get_host_state()
        if host_state != PRE_HOST:
            return False
        snap = self._coordinator.snapshot()
        if snap.phase != ELECTION_IDLE:
            return False
        if self._campaign_task is not None and not self._campaign_task.done():
            return False
        return True

    async def start_campaign(self) -> ElectionSnapshot | None:
        """Begin an election campaign if conditions allow.

        Returns the snapshot after starting, or None if not eligible.
        """
        if self._shutdown or not self._members:
            return None
        if self._get_host_state() != PRE_HOST:
            return None

        if self._campaign_task is not None and not self._campaign_task.done():
            # A campaign supervisor task is already running.
            return self._coordinator.snapshot()

        snap = self._coordinator.snapshot()
        if snap.phase == ELECTION_ELECTED:
            return snap

        if snap.phase == ELECTION_IDLE:
            try:
                snap = self._coordinator.begin_election()
            except SecurityStoreError as exc:
                error("Election could not begin.", {"error": str(exc)})
                return None

            info("Election campaign started.",
                 {"term": snap.term, "quorum": snap.quorum})

            if snap.phase == ELECTION_ELECTED:
                if self._on_elected is not None:
                    asyncio.create_task(self._on_elected(snap.term))
                return snap

        # Spawn campaign supervisor task
        self._campaign_task = asyncio.create_task(self._run_campaign())
        return self._coordinator.snapshot()

    async def _run_campaign(self) -> None:
        """Manage active campaign rounds and delayed retries while PRE_HOST."""
        identity = self._get_identity()

        try:
            while not self._shutdown:
                # 1. Check host state
                host_state = self._get_host_state()
                if host_state == FOLLOWING_HOST:
                    info("Host recovered during election; campaign stopped.")
                    snap = self._coordinator.snapshot()
                    self._coordinator.abort_campaign(snap.term)
                    return
                if host_state == HOST_CONFLICT:
                    warn("Host conflict during election; campaign stopped.")
                    snap = self._coordinator.snapshot()
                    self._coordinator.abort_campaign(snap.term)
                    return
                if host_state != PRE_HOST:
                    snap = self._coordinator.snapshot()
                    self._coordinator.abort_campaign(snap.term)
                    return

                # 2. Check coordinator state
                snap = self._coordinator.snapshot()
                if snap.phase == ELECTION_ELECTED:
                    return
                if snap.phase == ELECTION_IDLE:
                    try:
                        snap = self._coordinator.begin_election()
                    except SecurityStoreError as exc:
                        error("Election could not begin.", {"error": str(exc)})
                        return
                    if snap.phase == ELECTION_ELECTED:
                        return

                current_term = snap.term

                # 3. Intra-term retry loop
                campaign_won = False
                retries = 0
                while retries <= self._config.max_retries:
                    # Check host state each round
                    host_state = self._get_host_state()
                    if host_state != PRE_HOST:
                        self._coordinator.abort_campaign(current_term)
                        return

                    # Check if coordinator state changed
                    snap = self._coordinator.snapshot()
                    if snap.phase == ELECTION_ELECTED:
                        campaign_won = True
                        break
                    if snap.phase != ELECTION_CANDIDATE or snap.term != current_term:
                        # Superseded by higher term from someone else
                        break

                    # Broadcast requests
                    await self._broadcast_requests(identity, current_term)

                    # Wait for election result
                    try:
                        await asyncio.wait_for(
                            self._wait_for_election_result(current_term),
                            timeout=self._config.timeout_seconds,
                        )
                        snap = self._coordinator.snapshot()
                        if snap.phase == ELECTION_ELECTED:
                            campaign_won = True
                        break
                    except asyncio.TimeoutError:
                        retries += 1
                        if retries <= self._config.max_retries:
                            info("Election round timed out; retrying.",
                                 {"term": current_term, "retry": retries})
                            await asyncio.sleep(self._config.retry_delay_seconds)

                if campaign_won:
                    info("Election won; campaign supervisor completed.",
                         {"term": current_term})
                    if self._on_elected is not None:
                        asyncio.create_task(self._on_elected(current_term))
                    return

                # Check if won or superseded
                snap = self._coordinator.snapshot()
                if snap.phase == ELECTION_ELECTED:
                    return

                # Campaign for current_term finished without winning -> abort to IDLE
                self._coordinator.abort_campaign(current_term)

                # Check host state again
                if self._get_host_state() != PRE_HOST or self._shutdown:
                    return

                # Delay before next campaign (inter-campaign backoff)
                info("Election campaign term ended without quorum; scheduling retry.",
                     {"term": current_term})
                await asyncio.sleep(self._config.retry_delay_seconds)

        except asyncio.CancelledError:
            snap = self._coordinator.snapshot()
            if snap.phase == ELECTION_CANDIDATE:
                self._coordinator.abort_campaign(snap.term)
            raise

    async def _broadcast_requests(
        self, identity: Any, term: int,
    ) -> None:
        """Send election-request to every member with an active session."""
        for node_id in self._member_node_ids:
            if node_id == identity.node_id:
                continue  # Don't request a vote from self.
            session_id = self._get_session_id(node_id)
            if session_id is None:
                continue  # Peer not connected.
            try:
                payload = build_election_request_payload(
                    identity,
                    session_id,
                    term,
                    identity.node_id,
                    self._fingerprint,
                )
                await self._send_to_peer(node_id, payload)
            except (OSError, PeerProtocolError) as exc:
                warn("Failed to send election request.",
                     {"peer": node_id, "error": str(exc)})

    async def _wait_for_election_result(self, term: int) -> None:
        """Poll coordinator until election resolves or term changes."""
        while True:
            snap = self._coordinator.snapshot()
            if snap.phase == ELECTION_ELECTED:
                return
            if snap.phase == ELECTION_IDLE:
                return  # Superseded by higher term.
            if snap.term != term:
                return
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cancel_campaign(self) -> None:
        """Cancel any running campaign task and reset coordinator to IDLE."""
        if self._campaign_task is not None:
            self._campaign_task.cancel()
            try:
                await self._campaign_task
            except (asyncio.CancelledError, Exception):
                pass
            self._campaign_task = None
        snap = self._coordinator.snapshot()
        if snap.phase == ELECTION_CANDIDATE:
            self._coordinator.abort_campaign(snap.term)

    async def shutdown(self) -> None:
        """Cleanly shut down the election protocol."""
        self._shutdown = True
        await self.cancel_campaign()
