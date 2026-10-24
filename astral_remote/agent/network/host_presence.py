"""Host Presence state machine and monitoring policy (Phase 05).

This layer is deliberately separate from the generic peer heartbeat in
peer_network.py.  Agent heartbeats observe whether *any* peer is alive;
host presence observes whether the currently recognised Host is
responding to host-specific heartbeats.

The state machine is a pure, dependency-light tracker: it has no sockets
and no asyncio.  A caller (the PeerNetwork) feeds it with
:meth:`observe_ack`, :meth:`observe_failure`, and :meth:`observe_conflict`
events and asks it for a :class:`HostPresenceStatus` snapshot.

State transitions (Phase 05 scope):

- HOST_UNKNOWN    -> FOLLOWING_HOST  (first valid ack or explicit config)
- FOLLOWING_HOST  -> HOST_SUSPECTED  (one or more missed responses)
- HOST_SUSPECTED  -> PRE_HOST        (consecutive failures reach the threshold)
- HOST_SUSPECTED  -> FOLLOWING_HOST  (valid response returns; recovery)
- PRE_HOST        -> FOLLOWING_HOST  (valid response returns before election)
- any             -> HOST_CONFLICT   (conflicting host claims observed)

A Pre-Host is *not* an election winner and does not take over Host work.
It is only the state where the local Agent is ready to participate in a
future Phase 06 consensus election.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Any, Callable

from astral_remote.shared.logging_utils import warn

# Host presence states.  Names are deliberately descriptive and constant.
HOST_UNKNOWN = "HOST_UNKNOWN"
FOLLOWING_HOST = "FOLLOWING_HOST"
HOST_SUSPECTED = "HOST_SUSPECTED"
PRE_HOST = "PRE_HOST"
HOST_CONFLICT = "HOST_CONFLICT"

HOST_STATES = (HOST_UNKNOWN, FOLLOWING_HOST, HOST_SUSPECTED, PRE_HOST, HOST_CONFLICT)

# Default host monitoring policy.  Tuned for the existing Phase 04 peer
# heartbeat defaults (2 s interval, 1 s response timeout).
DEFAULT_HOST_HEARTBEAT_INTERVAL_SECONDS = 2.0
DEFAULT_HOST_HEARTBEAT_RESPONSE_TIMEOUT_SECONDS = 1.0
DEFAULT_HOST_FAILURE_THRESHOLD = 3
DEFAULT_HOST_RECOVERY_THRESHOLD = 1


def _utc_now_text() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class HostPresenceConfig:
    """Tunable host presence policy.

    `heartbeat_interval_seconds` is the interval between host heartbeat
    requests.  `response_timeout_seconds` is how long a request may wait
    for a matching ACK before it is counted as a missed response.
    `failure_threshold` is the consecutive missed-response count that
    declares a Host failure (PRE_HOST).  `recovery_threshold` is the
    consecutive valid-response count that restores FOLLOWING_HOST from
    PRE_HOST/HOST_SUSPECTED before any election has started.
    """

    heartbeat_interval_seconds: float = DEFAULT_HOST_HEARTBEAT_INTERVAL_SECONDS
    response_timeout_seconds: float = DEFAULT_HOST_HEARTBEAT_RESPONSE_TIMEOUT_SECONDS
    failure_threshold: int = DEFAULT_HOST_FAILURE_THRESHOLD
    recovery_threshold: int = DEFAULT_HOST_RECOVERY_THRESHOLD

    def validated(self) -> "HostPresenceConfig":
        if (isinstance(self.heartbeat_interval_seconds, bool)
                or not isinstance(self.heartbeat_interval_seconds, (int, float))
                or self.heartbeat_interval_seconds <= 0):
            raise ValueError("Host heartbeat interval must be a positive number.")
        if (isinstance(self.response_timeout_seconds, bool)
                or not isinstance(self.response_timeout_seconds, (int, float))
                or self.response_timeout_seconds <= 0):
            raise ValueError("Host heartbeat response timeout must be a positive number.")
        if (isinstance(self.failure_threshold, bool)
                or not isinstance(self.failure_threshold, int)
                or self.failure_threshold < 1):
            raise ValueError("Host failure threshold must be a positive integer.")
        if (isinstance(self.recovery_threshold, bool)
                or not isinstance(self.recovery_threshold, int)
                or self.recovery_threshold < 1):
            raise ValueError("Host recovery threshold must be a positive integer.")
        return self


def parse_host_presence_config(config: dict[str, Any] | None) -> HostPresenceConfig:
    """Parse the optional ``peerNetwork.hostPresence`` section.

    Example::

        {"peerNetwork": {"hostPresence": {
            "heartbeatIntervalSeconds": 2.0,
            "responseTimeoutSeconds": 1.0,
            "failureThreshold": 3,
            "recoveryThreshold": 1,
            "hostNodeId": "node-v1-..."     # optional explicit host
        }}}

    Invalid values raise ``ValueError`` so the caller can fail fast
    during configuration validation.
    """
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Peer network configuration is invalid.")
    section = raw.get("hostPresence")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("Host presence configuration is invalid.")
    return HostPresenceConfig(
        heartbeat_interval_seconds=section.get(
            "heartbeatIntervalSeconds", DEFAULT_HOST_HEARTBEAT_INTERVAL_SECONDS),
        response_timeout_seconds=section.get(
            "responseTimeoutSeconds", DEFAULT_HOST_HEARTBEAT_RESPONSE_TIMEOUT_SECONDS),
        failure_threshold=section.get(
            "failureThreshold", DEFAULT_HOST_FAILURE_THRESHOLD),
        recovery_threshold=section.get(
            "recoveryThreshold", DEFAULT_HOST_RECOVERY_THRESHOLD),
    ).validated()


@dataclass(frozen=True)
class HostPresenceStatus:
    """Read-only host monitoring snapshot."""

    state: str
    host_node_id: str | None
    consecutive_failures: int
    consecutive_successes: int
    last_ack_at: str | None
    last_ack_age_seconds: float | None
    transition_reason: str | None
    last_error: str | None


class HostPresenceTracker:
    """Pure state machine for host presence tracking.

    The tracker is *not* thread-safe and is expected to be driven from a
    single asyncio event loop.  All timing decisions are made by the
    caller using a monotonic clock; the tracker itself only records the
    wall-clock timestamp for diagnostics.

    The tracker does not know any peers beyond the single node it
    recognises as the current Host.  Conflicting claims put it into
    HOST_CONFLICT rather than silently selecting a winner, because
    Phase 05 never elects a Host.
    """

    def __init__(self, config: HostPresenceConfig | None = None, *,
                 monotonic: Callable[[], float] | None = None,
                 now_text: Callable[[], str] | None = None):
        self._config = config or HostPresenceConfig()
        self._config.validated()
        self._monotonic = monotonic or time.monotonic
        self._now_text = now_text or _utc_now_text
        self._state = HOST_UNKNOWN
        self._host_node_id: str | None = None
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_ack_time: float | None = None
        self._last_ack_wall: str | None = None
        self._transition_reason: str | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Public mutations
    # ------------------------------------------------------------------

    def configure_host(self, node_id: str) -> str:
        """Set the explicitly configured current Host (from config).

        This is an *operator-provided* host identity; it is not a vote or
        a consensus result.  If a different host was already observed the
        tracker enters HOST_CONFLICT because Phase 05 never picks a winner.
        Returns the new state.
        """
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("Host node id must be a non-empty string.")
        if self._host_node_id is not None and self._host_node_id != node_id:
            self._state = HOST_CONFLICT
            self._transition_reason = (
                f"Host identity changed from {self._host_node_id} to {node_id}")
            return self._state
        first = self._host_node_id is None
        self._host_node_id = node_id
        if first:
            # A configured host identity is a claim, not a live observation.
            # The state remains HOST_UNKNOWN until the first ACK.
            self._state = HOST_UNKNOWN
            self._transition_reason = "Host identity configured; waiting for first response"
        return self._state

    def observe_ack(self, node_id: str) -> str:
        """Record a valid host heartbeat ACK from `node_id`."""
        if not isinstance(node_id, str) or not node_id:
            self._record_error("Invalid host ACK node id.")
            return self._state
        if self._host_node_id is None:
            # First live observation establishes the host identity.
            self._host_node_id = node_id
            self._consecutive_failures = 0
            self._consecutive_successes = 1
            self._touch_ack()
            self._state = FOLLOWING_HOST
            self._transition_reason = "First valid host response observed"
            return self._state
        if self._host_node_id != node_id:
            self._state = HOST_CONFLICT
            self._transition_reason = (
                f"Host ACK from {node_id} does not match recognised host "
                f"{self._host_node_id}")
            return self._state
        # Matching host ACK.
        self._consecutive_failures = 0
        self._consecutive_successes += 1
        self._touch_ack()
        if self._state in (HOST_SUSPECTED, PRE_HOST):
            if self._consecutive_successes >= self._config.recovery_threshold:
                self._state = FOLLOWING_HOST
                self._transition_reason = (
                    f"Host recovered after {self._consecutive_successes} valid "
                    f"response(s) and {self._consecutive_failures} failure(s)")
                self._consecutive_successes = 0
        elif self._state == HOST_UNKNOWN:
            self._state = FOLLOWING_HOST
            self._transition_reason = "First valid host response observed"
        return self._state

    def observe_failure(self) -> str:
        """Record a missed host heartbeat response.

        Called by the monitor when the response timeout has elapsed with
        no matching ACK.  Returns the new state.
        """
        if self._host_node_id is None:
            # No host identity has ever been established.  We cannot claim
            # a host failed when we never observed one.
            return self._state
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        if self._state == HOST_UNKNOWN:
            # First response was already missed.  This is not yet a
            # failure of an established host; stay unknown until a
            # response is actually observed.
            self._transition_reason = (
                f"No host response observed yet ({self._consecutive_failures} missed)")
            return self._state
        if self._state in (FOLLOWING_HOST, HOST_SUSPECTED):
            if self._consecutive_failures >= self._config.failure_threshold:
                self._state = PRE_HOST
                self._transition_reason = (
                    f"Host failure detected after {self._consecutive_failures} "
                    f"consecutive missed responses")
                warn("Host failure detected; entering PRE_HOST.",
                     {"hostNodeId": self._host_node_id,
                      "failures": self._consecutive_failures})
            else:
                self._state = HOST_SUSPECTED
                self._transition_reason = (
                    f"Host response missed "
                    f"({self._consecutive_failures}/{self._config.failure_threshold})")
        return self._state

    def observe_conflict(self, message: str) -> str:
        """Record a conflicting host claim.  Never picks a winner."""
        self._state = HOST_CONFLICT
        self._transition_reason = str(message) or "Conflicting host claim observed"
        self._record_error(self._transition_reason)
        warn("Host presence conflict observed.", {"message": self._transition_reason})
        return self._state

    def reset(self) -> None:
        """Clear all host presence state (e.g. on network shutdown)."""
        self._state = HOST_UNKNOWN
        self._host_node_id = None
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_ack_time = None
        self._last_ack_wall = None
        self._transition_reason = None
        self._last_error = None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def host_node_id(self) -> str | None:
        return self._host_node_id

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def consecutive_successes(self) -> int:
        return self._consecutive_successes

    def status(self) -> HostPresenceStatus:
        """Return the current host presence snapshot."""
        now = self._monotonic()
        last_ack_age = (now - self._last_ack_time
                        if self._last_ack_time is not None else None)
        return HostPresenceStatus(
            state=self._state,
            host_node_id=self._host_node_id,
            consecutive_failures=self._consecutive_failures,
            consecutive_successes=self._consecutive_successes,
            last_ack_at=self._last_ack_wall,
            last_ack_age_seconds=last_ack_age,
            transition_reason=self._transition_reason,
            last_error=self._last_error,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _touch_ack(self) -> None:
        self._last_ack_time = self._monotonic()
        self._last_ack_wall = self._now_text()
        self._last_error = None

    def _record_error(self, message: str) -> None:
        self._last_error = str(message)