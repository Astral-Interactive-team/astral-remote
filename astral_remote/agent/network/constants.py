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

# Maximum assumed clock skew between election participants (seconds).
#
# RECEIVER-SIDE LEASE EXPIRATION & CLOCK-SKEW GUARANTEE (Phase 07)
# ----------------------------------------------------------------
# Authoritative Host agent-command messages carry an absolute wall-clock
# lease expiry timestamp (leaseExpiresAt), derived from the active Host
# authority lease at transmission time using time.time().
#
# The receiving Agent validates the deadline immediately before execution:
#
#   receiver_wall_clock < leaseExpiresAt - MAX_CLOCK_SKEW_SECONDS
#
# A command is rejected if receiver_wall_clock >= leaseExpiresAt - MAX_CLOCK_SKEW_SECONDS.
#
# Missing, non-numeric, boolean, or non-finite expiry values are rejected.
# However, structurally valid timestamps are NOT proof that clocks are
# synchronized. The application does NOT monitor Windows Time Service (W32Time)
# health, query NTP, or independently verify relative clock offsets.
#
# CONDITIONAL SAFETY & CLOCK-SKEW ASSUMPTION:
# Stale-command rejection after Host authority lease expiration is guaranteed
# ONLY while the explicit, bounded clock-skew assumption holds:
#
#   The receiver's wall clock must not lag behind the relevant Host's wall
#   clock by more than MAX_CLOCK_SKEW_SECONDS (5.0 s), and must not be adjusted
#   backward in a way that violates this assumption.
#
# BEHAVIOR UNDER CLOCK SKEW:
# 1. Receiver clock ahead of Host clock:
#    The acceptance deadline is reached early. Stale commands remain rejected,
#    though legitimate commands near expiry may be rejected prematurely
#    (fails closed with respect to stale commands).
# 2. Receiver clock lagging behind Host clock by <= MAX_CLOCK_SKEW_SECONDS:
#    The 5.0-second conservative deduction absorbs the lag. Commands arriving
#    after Host lease expiration are rejected (safe).
# 3. Receiver clock lagging behind Host clock by > MAX_CLOCK_SKEW_SECONDS:
#    FAILS OPEN. The receiver accepts delayed commands for up to
#    (lag - MAX_CLOCK_SKEW_SECONDS) seconds after the Host's lease has expired.
# 4. Backward wall-clock adjustments:
#    Stepping the receiver's wall clock backward can resurrect the acceptance
#    window for expired commands.
#
# DEPLOYMENT REQUIREMENTS:
# - All participating nodes must maintain a relative wall-clock skew no
#   greater than MAX_CLOCK_SKEW_SECONDS (5.0 s) for the security guarantee
#   to apply.
# - A configured or running Windows Time Service or NTP client is not, by
#   itself, proof that the bound is currently satisfied.
# - Clock synchronization loss, excessive drift, virtual-machine clock
#   changes, and backward clock adjustments can invalidate the guarantee.
# - Without a validated clock-skew bound, stale-command rejection after Host
#   lease expiration is not guaranteed.
# - Physical multi-machine partition and clock-divergence testing has not
#   yet been completed.
MAX_CLOCK_SKEW_SECONDS = 5.0
