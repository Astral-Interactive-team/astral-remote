# Read-only host facts for the managed #info channel and device registry.
# Returns hostname, machine ID, local IPv4, operating-system name, and
# process uptime. The hostname lets the operator tell managed computers
# apart; the full #info dashboard is built from windows/inventory.py. The
# OS name and process uptime also feed the shared device registry and the
# read-only status reply. Still exposes no usernames, SIDs, or local paths.

from __future__ import annotations

import platform
import socket
import time

_START = time.monotonic()
_WINDOWS_NT = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


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


def get_os_name() -> str:
    """Return a short Windows edition name for the device registry.

    The registry values are the same non-secret fields the inventory report
    shows. Off Windows, or when the registry cannot be read, a plain
    platform description is used. No username, path, or product key is
    included.
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_NT) as key:
            product, _ = winreg.QueryValueEx(key, "ProductName")
            try:
                display, _ = winreg.QueryValueEx(key, "DisplayVersion")
            except OSError:
                display = ""
    except (ImportError, OSError):
        product = ""
        display = ""
    name = " ".join(str(product or "").split())
    version = " ".join(str(display or "").split())
    if name and version:
        return f"{name} {version}"
    if name:
        return name
    try:
        fallback = platform.platform()
    except Exception:  # noqa: BLE001 - platform metadata is optional
        fallback = ""
    return " ".join(str(fallback or "").split()) or "Unavailable"


def get_uptime_seconds() -> int:
    """Return whole seconds since this Agent process started."""
    return int(time.monotonic() - _START)

