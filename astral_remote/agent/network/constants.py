"""Constants shared by the peer networking implementation."""

# Protocol configuration
PROTOCOL_VERSION = 1
NETWORK_NAME = "astral-remote"
MAX_MESSAGE_BYTES = 16_384
CONNECT_TIMEOUT_SECONDS = 5

# Peer heartbeat timing
DISCONNECT_STATUS_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_RESPONSE_TIMEOUT_SECONDS = 1.0

# Peer heartbeat states
_HEARTBEAT_STALE = "stale"
_HEARTBEAT_ACTIVE = "active"
_HEARTBEAT_WAITING = "waiting"
_HEARTBEAT_FAILED = "failed"

# Maximum heartbeat attempts before closing the session
_HEARTBEAT_MAX_RETRIES = 3
