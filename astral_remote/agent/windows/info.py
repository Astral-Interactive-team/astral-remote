# Read-only host facts for the managed #info channel.
# Returns hostname, machine ID, local IPv4, and process uptime. The
# hostname lets the operator tell managed computers apart; the full #info
# dashboard is built from windows/inventory.py. Still exposes no
# usernames, SIDs, or local paths.

from __future__ import annotations

import platform
import socket
import time

_START = time.monotonic()


def get_hostname() -> str:
    """Return this computer's network name for operator identification."""
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    if not name:
        try:
            name = platform.node()
        except Exception:
            name = ""
    return name or "unknown-host"


def get_local_ip() -> str:
    """Return the primary local IPv4 for operator reference (informational only).

    Uses a UDP connect that sends no packets; the numeric address needs
    no DNS lookup. Never used as identity: DHCP/VPN/NAT changes it, and
    machines behind the same NAT share public IPs.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "unknown-ip"
    return ip or "unknown-ip"


def get_machine_id() -> str:
    """Return a short stable ID unique to this Windows installation.

    Hostnames can collide (default DESKTOP- names, cloned VMs, separate
    networks), so status appends the first 8 hex chars of the Windows
    MachineGuid. The full GUID is never exposed; only the short suffix
    travels in Discord messages. Falls back to unknown-id off Windows.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
    except (ImportError, OSError):
        return "unknown-id"
    short = "".join(ch for ch in str(guid).lower() if ch in "0123456789abcdef")[:8]
    return short or "unknown-id"


def get_uptime_seconds() -> int:
    """Return whole seconds since this Agent process started."""
    return int(time.monotonic() - _START)

