"""Session state for an authenticated peer connection."""

import asyncio
import datetime
import time


def _utc_now_text() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


class PeerSession:
    """One mutually authenticated peer connection and its heartbeat state."""

    __slots__ = ("node_id", "session_id", "reader", "writer", "interval",
                 "response_timeout", "send_lock", "next_sequence",
                 "pending_sequence", "pending_since", "host_pending_sequence",
                 "host_pending_since", "session_started", "last_heartbeat_time",
                 "last_heartbeat_wall", "last_heartbeat_sequence",
                 "last_received_time", "last_received_wall",
                 "last_received_sequence", "last_ack_time", "last_ack_wall",
                 "last_ack_sequence", "last_host_ack_time", "last_host_ack_wall",
                 "last_host_ack_sequence", "last_error", "last_error_wall",
                 "active")

    def __init__(self, node_id: str, session_id: str, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, interval: float, response_timeout: float):
        self.node_id = node_id
        self.session_id = session_id
        self.reader = reader
        self.writer = writer
        self.interval = interval
        self.response_timeout = response_timeout
        self.send_lock = asyncio.Lock()
        self.next_sequence = 0
        self.pending_sequence: int | None = None
        self.pending_since: float | None = None
        self.host_pending_sequence: int | None = None
        self.host_pending_since: float | None = None
        self.session_started = time.monotonic()
        self.last_heartbeat_time: float | None = None
        self.last_heartbeat_wall: str | None = None
        self.last_heartbeat_sequence: int | None = None
        self.last_received_time: float | None = None
        self.last_received_wall: str | None = None
        self.last_received_sequence: int | None = None
        self.last_ack_time: float | None = None
        self.last_ack_wall: str | None = None
        self.last_ack_sequence: int | None = None
        self.last_host_ack_time: float | None = None
        self.last_host_ack_wall: str | None = None
        self.last_host_ack_sequence: int | None = None
        self.last_error: str | None = None
        self.last_error_wall: str | None = None
        self.active = True

    def touch_sent(self, sequence: int) -> None:
        """Record that heartbeat `sequence` was sent with a monotonic stamp."""
        self.last_heartbeat_time = time.monotonic()
        self.last_heartbeat_wall = _utc_now_text()
        self.last_heartbeat_sequence = sequence
        self.pending_sequence = sequence
        self.pending_since = self.last_heartbeat_time

    def touch_received(self, sequence: int) -> None:
        """Record a valid inbound heartbeat with a monotonic stamp."""
        self.last_received_time = time.monotonic()
        self.last_received_wall = _utc_now_text()
        self.last_received_sequence = sequence

    def touch_ack(self, sequence: int) -> None:
        """Record a matching valid ACK; clears the pending request."""
        self.last_ack_time = time.monotonic()
        self.last_ack_wall = _utc_now_text()
        self.last_ack_sequence = sequence
        self.pending_sequence = None
        self.pending_since = None
        self.last_error = None
        self.last_error_wall = None

    def touch_host_sent(self, sequence: int) -> None:
        """Record that a host heartbeat was sent."""
        self.host_pending_sequence = sequence
        self.host_pending_since = time.monotonic()

    def touch_host_ack(self, sequence: int) -> None:
        """Record a valid host heartbeat ACK; clears the pending host request."""
        self.last_host_ack_time = time.monotonic()
        self.last_host_ack_wall = _utc_now_text()
        self.last_host_ack_sequence = sequence
        self.host_pending_sequence = None
        self.host_pending_since = None
        self.last_error = None
        self.last_error_wall = None

    def record_error(self, message: str) -> None:
        self.last_error = str(message) or None
        self.last_error_wall = _utc_now_text()

    def close(self) -> None:
        self.active = False
