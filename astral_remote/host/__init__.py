"""Host-only runtime components.

Importing this package has no Discord connection side effects.
"""

from astral_remote.agent.network.host_role import (
    ACTIVE,
    ACTIVATING,
    FENCED,
    HOST_ROLES,
    HostAuthorityConfig,
    HostLifecycleManager,
    HostRoleStatus,
    LeaseGrant,
    STANDBY,
    STEPPING_DOWN,
)
from astral_remote.host.runtime import HostRuntime

__all__ = [
    "ACTIVE",
    "ACTIVATING",
    "FENCED",
    "HOST_ROLES",
    "HostAuthorityConfig",
    "HostLifecycleManager",
    "HostRoleStatus",
    "HostRuntime",
    "LeaseGrant",
    "STANDBY",
    "STEPPING_DOWN",
]
