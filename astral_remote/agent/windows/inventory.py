# Read-only Windows system inventory collector for the #info dashboard.
# Gathers every fact the Components V2 report renders, without changing
# system state: registry reads, ctypes calls for CPU/memory/power, and one
# PowerShell CIM pass for hardware facts that need it. Each part is guarded
# separately, so one failing probe only marks its own fields Unavailable
# and never aborts the rest.
# Values are returned display-ready (already formatted strings) so the
# renderer stays a pure layout step. No secrets, tokens, product keys, or
# command lines are read; the disk and security probes are read-only. Process,
# service, and installed-software inventories are deliberately not collected,
# because the report does not show them.
# Every child process is spawned with CREATE_NO_WINDOW so a --noconsole
# Agent never flashes a console window while collecting.

from __future__ import annotations

import ctypes
import datetime
import json
import os
import platform
import socket
import subprocess
import time
from pathlib import Path

from astral_remote.shared.winproc import hidden_kwargs

UNAVAILABLE = "Unavailable"
NOT_SUPPORTED = "Not supported"
NOT_APPLICABLE = "N/A"

PS_TIMEOUT_S = 120.0
PUBLIC_IP_TIMEOUT_S = 3.0
CPU_SAMPLE_SECONDS = 0.25
MAX_PS_STDOUT = 32 * 1024 * 1024
PUBLIC_IP_ENDPOINTS = ("https://api.ipify.org", "https://icanhazip.com")

# One CIM pass. Every section is wrapped in try/catch so a missing module
# or denied provider only nulls that key, and the command is transferred
# as base64 UTF-16LE so console code pages never corrupt the script.
_PS_INVENTORY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$o = [ordered]@{}
try { $o.os = Get-CimInstance -ClassName Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture, OperatingSystemSKU, ProductType, BuildType, WindowsDirectory, SystemDirectory, BootDevice, Locale, Manufacturer, TotalVisibleMemorySize, FreePhysicalMemory, TotalVirtualMemorySize, FreeVirtualMemory, SizeStoredInPagingFiles, CSName, @{n='InstallDate';e={try{$_.InstallDate.ToString('yyyy-MM-dd HH:mm:ss')}catch{''}}}, @{n='LastBootUpTime';e={try{$_.LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss')}catch{''}}} } catch {}
try { $o.computer = Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object Manufacturer, Model, SystemType, TotalPhysicalMemory, Domain, PartOfDomain, Workgroup, NumberOfProcessors, NumberOfLogicalProcessors, PCSystemType } catch {}
try { $o.bios = Get-CimInstance -ClassName Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion, Version, @{n='ReleaseDate';e={try{$_.ReleaseDate.ToString('yyyy-MM-dd')}catch{''}}} } catch {}
try { $o.board = Get-CimInstance -ClassName Win32_BaseBoard | Select-Object Manufacturer, Product, Version } catch {}
try { $o.cpu = @(Get-CimInstance -ClassName Win32_Processor | Select-Object Name, Manufacturer, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed, CurrentClockSpeed, L2CacheSize, L3CacheSize, Architecture, AddressWidth, VirtualizationFirmwareEnabled, SecondLevelAddressTranslationExtensions, VMMonitorModeExtensions, LoadPercentage) } catch {}
try { $o.memoryModules = @(Get-CimInstance -ClassName Win32_PhysicalMemory | Select-Object Manufacturer, PartNumber, Capacity, Speed, ConfiguredClockSpeed, SMBIOSMemoryType, BankLabel, DeviceLocator) } catch {}
try { $o.pageFiles = @(Get-CimInstance -ClassName Win32_PageFileUsage | Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage) } catch {}
try { $o.gpus = @(Get-CimInstance -ClassName Win32_VideoController | Select-Object Name, AdapterCompatibility, DriverVersion, @{n='DriverDate';e={try{$_.DriverDate.ToString('yyyy-MM-dd')}catch{''}}}, AdapterRAM, CurrentHorizontalResolution, CurrentVerticalResolution, CurrentRefreshRate, VideoProcessor, Status) } catch {}
try { $o.logicalDisks = @(Get-CimInstance -ClassName Win32_LogicalDisk | Select-Object DeviceID, DriveType, FileSystem, FileSystemLabel, Size, FreeSpace, VolumeSerialNumber) } catch {}
try { $o.physicalDisks = @(Get-PhysicalDisk | Select-Object FriendlyName, MediaType, BusType, Size, HealthStatus, OperationalStatus, Model, FirmwareVersion) } catch {}
try { $o.diskHealth = @(Get-PhysicalDisk | ForEach-Object { $c = $_ | Get-StorageReliabilityCounter; [pscustomobject]@{ FriendlyName = $_.FriendlyName; Temperature = $c.Temperature; Wear = $c.Wear; ReadErrorsTotal = $c.ReadErrorsTotal } }) } catch {}
try { $o.bitlocker = @(Get-BitLockerVolume | Select-Object MountPoint, VolumeStatus, ProtectionStatus, EncryptionPercentage, EncryptionMethod) } catch {}
try { $o.adapters = @(Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, LinkSpeed, MacAddress, Virtual, ifIndex, MediaType, DriverVersion, DriverProvider) } catch {}
try { $o.ipAddresses = @(Get-NetIPAddress | Select-Object InterfaceIndex, IPAddress, PrefixLength, AddressFamily, AddressState) } catch {}
try { $o.routes = @(Get-NetRoute | Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } | Select-Object InterfaceIndex, NextHop, RouteMetric) } catch {}
try { $o.dns = @(Get-DnsClientServerAddress | Select-Object InterfaceIndex, AddressFamily, ServerAddresses) } catch {}
try { $o.ipInterfaces = @(Get-NetIPInterface | Select-Object InterfaceIndex, Dhcp, ConnectionState, AddressFamily) } catch {}
try { $o.adapterStats = @(Get-NetAdapterStatistics | Select-Object Name, ReceivedBytes, SentBytes) } catch {}
try { $o.adapterConfig = @(Get-CimInstance -ClassName Win32_NetworkAdapterConfiguration | Where-Object { $_.IPEnabled } | Select-Object Index, Description, DHCPServer, DHCPEnabled, MACAddress, IPAddress, IPSubnet, DefaultIPGateway, DNSServerSearchOrder) } catch {}
try { $o.battery = Get-CimInstance -ClassName Win32_Battery | Select-Object Name, BatteryStatus, EstimatedChargeRemaining, DesignCapacity, FullChargeCapacity } catch {}
try { $o.powerPlan = Get-CimInstance -Namespace root\cimv2\power -ClassName Win32_PowerPlan | Where-Object { $_.IsActive } | Select-Object -First 1 ElementName, InstanceID } catch {}
try { $o.tpm = Get-CimInstance -Namespace root\cimv2\security\microsofttpm -ClassName Win32_Tpm | Select-Object IsEnabled_InitialValue, IsActivated_InitialValue, IsOwned_InitialValue, SpecVersion, ManufacturerVersion } catch {}
try { $o.secureBoot = Confirm-SecureBootUEFI } catch {}
try { $o.defender = Get-MpComputerStatus | Select-Object AntivirusEnabled, RealTimeProtectionEnabled, AntispywareEnabled, BehaviorMonitorEnabled, IoavProtectionEnabled, NISEnabled, AMServiceEnabled, AntivirusSignatureVersion, @{n='AntivirusSignatureLastUpdated';e={try{$_.AntivirusSignatureLastUpdated.ToString('yyyy-MM-dd HH:mm:ss')}catch{''}}} } catch {}
try { $o.firewall = @(Get-NetFirewallProfile | Select-Object Name, Enabled) } catch {}
try { $o.hotfixes = @(Get-CimInstance -ClassName Win32_QuickFixEngineering | Select-Object HotFixID, Description, @{n='InstalledOn';e={try{$_.InstalledOn.ToString('yyyy-MM-dd')}catch{''}}}) } catch {}
try { $o.diskPerf = @(Get-CimInstance -ClassName Win32_PerfFormattedData_PerfDisk_PhysicalDisk | Select-Object Name, DiskReadBytesPerSec, DiskWriteBytesPerSec, PercentDiskTime) } catch {}
try { $o.netPerf = @(Get-CimInstance -ClassName Win32_PerfFormattedData_Tcpip_NetworkInterface | Select-Object Name, BytesReceivedPersec, BytesSentPersec, BytesTotalPersec) } catch {}
try { $o.thermal = @(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | Select-Object InstanceName, CurrentTemperature) } catch {}
try { $o.culture = Get-Culture | Select-Object Name, DisplayName } catch {}
try { $o.timeZone = Get-TimeZone | Select-Object Id, DisplayName } catch {}
$json = $o | ConvertTo-Json -Depth 5 -Compress
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
"""


# ---------------------------------------------------------------------------
# Value formatting helpers (display-ready strings)
# ---------------------------------------------------------------------------


def format_bytes(value) -> str:
    """Render a byte count with binary units, or Unavailable when unknown."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return UNAVAILABLE
    if size < 0:
        return UNAVAILABLE
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024.0
        if size < 1024.0 or unit == "PB":
            return f"{size:.1f} {unit}"
    return UNAVAILABLE


def format_megabytes(value) -> str:
    """Render a megabyte count from WMI (used by page file usage)."""
    try:
        return format_bytes(float(value) * 1024 * 1024)
    except (TypeError, ValueError):
        return UNAVAILABLE


def format_percent(value) -> str:
    """Render a 0-100 number as a percentage, or Unavailable when unknown."""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return UNAVAILABLE


def format_duration(seconds) -> str:
    """Render a second count as a compact duration, or Unavailable."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return UNAVAILABLE
    if total < 0:
        return UNAVAILABLE
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


_KNOWN_MOMENT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


def format_moment(value) -> str:
    """Render a timestamp in a fixed layout, normalizing WMI/registry styles."""
    if value is None:
        return UNAVAILABLE
    text = str(value).strip()
    if not text:
        return UNAVAILABLE
    for pattern in _KNOWN_MOMENT_FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d %H:%M:%S") if "%H" in pattern else parsed.strftime("%Y-%m-%d")
    return text


def format_text(value) -> str:
    """Render a free-text fact, collapsing blanks into Unavailable."""
    if value is None:
        return UNAVAILABLE
    text = str(value).strip()
    return text or UNAVAILABLE


def _pick(*values) -> str:
    """Return the first value that is present and not already Unavailable.

    Local registry facts are pre-formatted strings, so a plain `or`
    chain would treat the literal marker as a real value.
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text and text != UNAVAILABLE:
                return text
            continue
        text = format_text(value)
        if text != UNAVAILABLE:
            return text
    return UNAVAILABLE


def format_flag(value) -> str:
    """Render a bool-ish fact as Yes/No/Unavailable."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return "Yes" if value else "No"
    return UNAVAILABLE


def _now(now=None) -> datetime.datetime:
    return now if now is not None else datetime.datetime.now()


# ---------------------------------------------------------------------------
# Low-level probes: registry, ctypes, PowerShell
# ---------------------------------------------------------------------------


def registry_value(root_name: str, path: str, name: str):
    """Read one registry value. Returns None when absent or unavailable."""
    try:
        import winreg

        root = getattr(winreg, root_name, None)
        if root is None:
            return None
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except (ImportError, OSError, AttributeError):
        return None


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SystemPowerStatus(ctypes.Structure):
    # Lead fields are BYTE, so they must be unsigned: 255 means "unknown".
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _kernel32():
    """Return kernel32 or None when not on Windows."""
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None


def read_uptime_seconds() -> float | None:
    """Return seconds since boot from GetTickCount64, or None."""
    kernel = _kernel32()
    if kernel is None:
        return None
    try:
        return kernel.GetTickCount64() / 1000.0
    except (AttributeError, OSError):
        return None


def read_memory_status() -> dict | None:
    """Return Windows memory counters, or None when unavailable."""
    kernel = _kernel32()
    if kernel is None:
        return None
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {
            "loadPercent": float(status.dwMemoryLoad),
            "totalPhysical": int(status.ullTotalPhys),
            "availablePhysical": int(status.ullAvailPhys),
            "commitLimit": int(status.ullTotalPageFile),
            "commitAvailable": int(status.ullAvailPageFile),
        }
    except (AttributeError, OSError):
        return None


def read_power_status() -> dict | None:
    """Return AC/battery state from GetSystemPowerStatus, or None."""
    kernel = _kernel32()
    if kernel is None:
        return None
    try:
        status = _SystemPowerStatus()
        if not kernel.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        return {
            "acLine": int(status.ACLineStatus),
            "batteryFlag": int(status.BatteryFlag),
            "batteryPercent": int(status.BatteryLifePercent),
            "batterySeconds": int(status.BatteryLifeTime),
        }
    except (AttributeError, OSError):
        return None


def read_cpu_utilization(window: float = CPU_SAMPLE_SECONDS) -> float | None:
    """Sample total CPU utilization over a short window, or None.

    Compares two GetSystemTimes snapshots; the kernel total already
    includes idle time, so utilization is one minus the idle ratio.
    """
    kernel = _kernel32()
    if kernel is None:
        return None

    def snapshot():
        try:
            idle, kern, user = _FileTime(), _FileTime(), _FileTime()
            if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                return None
            combine = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # noqa: E731
            return combine(idle), combine(kern), combine(user)
        except (AttributeError, OSError):
            return None

    first = snapshot()
    if first is None:
        return None
    time.sleep(max(0.0, float(window)))
    second = snapshot()
    if second is None:
        return None
    idle_delta = second[0] - first[0]
    total_delta = (second[1] - first[1]) + (second[2] - first[2])
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))


def read_public_ipv4(timeout: float = PUBLIC_IP_TIMEOUT_S) -> str | None:
    """Look up the public IPv4 through a public echo service, or None.

    This is the only outbound request the collector makes. It sends no
    credentials, no hostname, and no identifiers beyond the HTTP request
    itself, uses a short timeout, and never raises. Disabled behaviour is
    simply an Unavailable field when the lookup fails.
    """
    import urllib.request

    for endpoint in PUBLIC_IP_ENDPOINTS:
        try:
            request = urllib.request.Request(endpoint, headers={"User-Agent": "AstralRemote-Agent"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read(128).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001 - any network failure means Unavailable
            continue
        if _is_ipv4(text):
            return text
    return None


def _is_ipv4(text: str) -> bool:
    parts = str(text).split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return False
    return True


def _reachable(host: str = "1.1.1.1", port: int = 53, timeout: float = 1.5) -> bool | None:
    """Probe outbound connectivity with a TCP connect. None when not testable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_powershell_json(script: str, timeout: float = PS_TIMEOUT_S, runner=None) -> dict:
    """Run one base64-encoded PowerShell script and parse its JSON reply.

    The script prints base64 UTF-8 JSON so console code pages cannot
    corrupt non-ASCII hardware names. Any failure returns an empty dict;
    the caller marks the affected fields Unavailable.
    """
    import base64

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]
    execute = runner if runner is not None else subprocess.run
    try:
        if runner is not None:
            payload = execute(command)
        else:
            # CREATE_NO_WINDOW keeps the console child invisible; without
            # it a --noconsole Agent flashes a black PowerShell window.
            proc = execute(
                command, capture_output=True, shell=False, timeout=timeout,
                **hidden_kwargs(),
            )
            payload = proc.stdout or b""
    except Exception:  # noqa: BLE001 - timeouts and spawn failures are both Unavailable
        return {}
    if isinstance(payload, str):
        payload = payload.encode("ascii", errors="ignore")
    payload = b"".join(payload.split())[:MAX_PS_STDOUT]
    if not payload:
        return {}
    try:
        decoded = base64.b64decode(payload).decode("utf-8", errors="replace")
        parsed = json.loads(decoded)
    except Exception:  # noqa: BLE001 - malformed output means Unavailable
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Local (registry / ctypes / stdlib) facts
# ---------------------------------------------------------------------------

_WINDOWS_NT = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
_BIOS_KEY = r"HARDWARE\DESCRIPTION\System\BIOS"
_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_UAC_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
_SECUREBOOT_KEY = r"SYSTEM\CurrentControlSet\Control\SecureBoot\State"


def _local_facts(env=None, now=None) -> dict:
    """Collect registry, ctypes, and stdlib facts that need no subprocess."""
    source = env if env is not None else os.environ
    system_root = str(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "SystemRoot") or source.get("SystemRoot") or "")
    uptime = read_uptime_seconds()
    boot_time = None
    if uptime is not None:
        boot_time = (_now(now) - datetime.timedelta(seconds=uptime)).strftime("%Y-%m-%d %H:%M:%S")
    install_epoch = registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "InstallDate")
    install_date = None
    if isinstance(install_epoch, int) and install_epoch > 0:
        try:
            install_date = datetime.datetime.fromtimestamp(install_epoch).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            install_date = None
    return {
        "osName": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "ProductName")),
        "osEdition": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "EditionID")),
        "osDisplayVersion": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "DisplayVersion")),
        "osVersion": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "CurrentVersion")),
        "osBuild": _os_build(),
        "osManufacturer": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "Manufacturer")),
        "registeredOwner": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "RegisteredOwner")),
        "registeredOrganization": format_text(
            registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "RegisteredOrganization")
        ),
        "productId": format_text(registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "ProductId")),
        "installDate": install_date or UNAVAILABLE,
        "windowsDirectory": format_text(system_root),
        "systemDirectory": format_text(str(Path(system_root) / "System32")) if system_root else UNAVAILABLE,
        "systemManufacturer": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "SystemManufacturer")),
        "systemProduct": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "SystemProductName")),
        "systemFamily": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "SystemFamily")),
        "baseBoardManufacturer": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "BaseBoardManufacturer")),
        "baseBoardProduct": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "BaseBoardProduct")),
        "biosVendor": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "BIOSVendor")),
        "biosVersion": format_text(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "BIOSVersion")),
        "biosReleaseDate": format_moment(registry_value("HKEY_LOCAL_MACHINE", _BIOS_KEY, "BIOSReleaseDate")),
        "processorName": format_text(registry_value("HKEY_LOCAL_MACHINE", _CPU_KEY, "ProcessorNameString")),
        "processorVendor": format_text(registry_value("HKEY_LOCAL_MACHINE", _CPU_KEY, "VendorIdentifier")),
        "processorIdentifier": format_text(registry_value("HKEY_LOCAL_MACHINE", _CPU_KEY, "Identifier")),
        "processorMhz": registry_value("HKEY_LOCAL_MACHINE", _CPU_KEY, "~MHz"),
        "processorArchitecture": format_text(source.get("PROCESSOR_ARCHITECTURE")),
        "firmwareType": _firmware_type(),
        "uacEnabled": format_flag(registry_value("HKEY_LOCAL_MACHINE", _UAC_KEY, "EnableLUA")),
        "uacPrompt": _uac_prompt(),
        "bootTime": boot_time or UNAVAILABLE,
        "uptime": format_duration(uptime) if uptime is not None else UNAVAILABLE,
        "uptimeSeconds": uptime,
    }


def _os_build() -> str:
    """Compose the Windows build string from CurrentBuildNumber + UBR."""
    build = registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "CurrentBuildNumber")
    revision = registry_value("HKEY_LOCAL_MACHINE", _WINDOWS_NT, "UBR")
    if build is None:
        return UNAVAILABLE
    if isinstance(revision, int):
        return f"{build}.{revision}"
    return str(build)


def _firmware_type() -> str:
    """Infer the firmware type from the UEFI-only Secure Boot state key.

    Windows creates HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State
    only when the machine booted UEFI firmware, so a missing key means a
    legacy BIOS boot. The value is an inference, never a guess at the
    firmware vendor.
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SECUREBOOT_KEY):
            return "UEFI"
    except (ImportError, OSError, AttributeError):
        return "Legacy BIOS"


_UAC_PROMPTS = {
    0: "Elevate without prompting",
    1: "Prompt for credentials on the secure desktop",
    2: "Prompt for consent on the secure desktop",
    3: "Prompt for credentials",
    4: "Prompt for consent",
    5: "Prompt for consent for non-Windows binaries",
}


def _uac_prompt() -> str:
    """Render the admin consent prompt behaviour registry value."""
    value = registry_value("HKEY_LOCAL_MACHINE", _UAC_KEY, "ConsentPromptBehaviorAdmin")
    if isinstance(value, int) and value in _UAC_PROMPTS:
        return f"{value} - {_UAC_PROMPTS[value]}"
    return UNAVAILABLE


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _entry(ps: dict, key: str):
    """Return one CIM object from the PowerShell reply, or None."""
    value = ps.get(key)
    return value if isinstance(value, dict) else None


def _entries(ps: dict, key: str) -> list:
    """Return one CIM collection from the PowerShell reply as a list."""
    value = ps.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


_DRIVE_TYPES = {0: "Unknown", 1: "No root directory", 2: "Removable", 3: "Local disk", 4: "Network", 5: "CD-ROM", 6: "RAM disk"}
_PROCESSOR_ARCH = {0: "x86", 1: "MIPS", 2: "Alpha", 3: "PowerPC", 5: "ARM", 6: "IA64", 9: "x64", 12: "ARM64"}


def build_os_section(local: dict, ps: dict) -> dict:
    """Collect the Windows and operating system facts."""
    os_info = _entry(ps, "os") or {}
    computer = _entry(ps, "computer") or {}
    culture = _entry(ps, "culture") or {}
    time_zone = _entry(ps, "timeZone") or {}
    return {
        "osName": _pick(os_info.get("Caption"), local.get("osName")),
        "osManufacturer": _pick(local.get("osManufacturer"), os_info.get("Manufacturer")),
        "osVersion": _pick(os_info.get("Version"), local.get("osVersion")),
        "osBuild": _pick(local.get("osBuild"), os_info.get("BuildNumber")),
        "osDisplayVersion": format_text(local.get("osDisplayVersion")),
        "osEdition": format_text(local.get("osEdition")),
        "osArchitecture": format_text(os_info.get("OSArchitecture") or local.get("processorArchitecture")),
        "osConfiguration": _os_configuration(computer, os_info),
        "osBuildType": format_text(os_info.get("BuildType")),
        "registeredOwner": local.get("registeredOwner", UNAVAILABLE),
        "registeredOrganization": local.get("registeredOrganization", UNAVAILABLE),
        "productId": local.get("productId", UNAVAILABLE),
        "originalInstallDate": format_moment(os_info.get("InstallDate")) if not local.get("installDate") else local["installDate"],
        "bootTime": local.get("bootTime", UNAVAILABLE),
        "uptime": local.get("uptime", UNAVAILABLE),
        "systemLocale": _system_locale(culture, os_info),
        "inputLocale": format_text(culture.get("Name")),
        "timeZone": _time_zone(time_zone),
        "domainWorkgroup": format_text(computer.get("Domain") or computer.get("Workgroup")),
        "partOfDomain": format_flag(computer.get("PartOfDomain")),
        "windowsDirectory": _pick(os_info.get("WindowsDirectory"), local.get("windowsDirectory")),
        "systemDirectory": _pick(os_info.get("SystemDirectory"), local.get("systemDirectory")),
        "bootDevice": format_text(os_info.get("BootDevice")),
        "firmwareArchitecture": _pick(computer.get("SystemType"), local.get("processorArchitecture")),
    }


def _os_configuration(computer: dict, os_info: dict) -> str:
    """Describe the OS product/configuration from CIM values."""
    product_type = _int_or_none(os_info.get("ProductType"))
    if product_type == 1:
        return "Workstation"
    if product_type == 2:
        return "Domain controller"
    if product_type == 3:
        return "Server"
    if computer.get("PartOfDomain") is True:
        return "Domain member"
    if computer.get("PartOfDomain") is False:
        return "Standalone"
    return UNAVAILABLE


def _system_locale(culture: dict, os_info: dict) -> str:
    """Prefer the culture object, then the WMI locale id, for the system locale."""
    display = culture.get("DisplayName")
    if isinstance(display, str) and display.strip():
        return display.strip()
    locale = os_info.get("Locale")
    if isinstance(locale, str) and locale.strip():
        return locale.strip()
    return UNAVAILABLE


def _time_zone(time_zone: dict) -> str:
    """Render the active time zone id and display name."""
    identifier = time_zone.get("Id")
    display = time_zone.get("DisplayName")
    if isinstance(identifier, str) and identifier.strip() and isinstance(display, str) and display.strip():
        return f"{identifier.strip()} ({display.strip()})"
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    return UNAVAILABLE


def build_cpu_section(local: dict, ps: dict, utilization: float | None) -> dict:
    """Collect the processor facts, including optional sensors."""
    cpus = _entries(ps, "cpu")
    first = cpus[0] if cpus else {}
    cores = [item for item in (_int_or_none(cpu.get("NumberOfCores")) for cpu in cpus) if item is not None]
    physical = sum(cores) if cores else None
    logical = sum(
        item for item in (_int_or_none(cpu.get("NumberOfLogicalProcessors")) for cpu in cpus) if item is not None
    ) if cpus else None
    return {
        "manufacturer": _pick(first.get("Manufacturer"), local.get("processorVendor")),
        "model": _pick(first.get("Name"), local.get("processorName")),
        "identifier": local.get("processorIdentifier", UNAVAILABLE),
        "architecture": _processor_architecture(first, local),
        "physicalCores": str(physical) if physical else UNAVAILABLE,
        "logicalProcessors": str(logical) if logical else UNAVAILABLE,
        "maxClock": _mhz(first.get("MaxClockSpeed")) if first else _mhz(local.get("processorMhz")),
        "currentClock": _mhz(first.get("CurrentClockSpeed")),
        "l2Cache": _kb(first.get("L2CacheSize")),
        "l3Cache": _kb(first.get("L3CacheSize")),
        "virtualizationFirmware": format_flag(first.get("VirtualizationFirmwareEnabled")),
        "virtualizationExtensions": format_flag(first.get("SecondLevelAddressTranslationExtensions")),
        "hypervisorPresent": format_flag(first.get("VMMonitorModeExtensions")),
        "utilization": format_percent(utilization) if utilization is not None else UNAVAILABLE,
        "temperature": _cpu_temperature(ps),
    }


def _processor_architecture(cpu: dict, local: dict) -> str:
    """Render the processor architecture, preferring the CIM enum name."""
    value = _int_or_none(cpu.get("Architecture"))
    if value is not None and value in _PROCESSOR_ARCH:
        return _PROCESSOR_ARCH[value]
    return format_text(local.get("processorArchitecture"))


def _mhz(value) -> str:
    mhz = _int_or_none(value)
    return f"{mhz} MHz ({mhz / 1000:.2f} GHz)" if mhz else UNAVAILABLE


def _kb(value) -> str:
    kb = _int_or_none(value)
    return f"{kb} KB" if kb else UNAVAILABLE


def _cpu_temperature(ps: dict) -> str:
    """Convert ACPI thermal-zone readings (tenths of Kelvin) to Celsius."""
    zones = _entries(ps, "thermal")
    readings = []
    for zone in zones:
        raw = _int_or_none(zone.get("CurrentTemperature"))
        if raw is None or raw <= 0:
            continue
        celsius = raw / 10.0 - 273.15
        if -50.0 <= celsius <= 150.0:
            readings.append(celsius)
    if not readings:
        return UNAVAILABLE
    return f"{max(readings):.1f} C"


def build_memory_section(ps: dict, memory: dict | None) -> dict:
    """Collect the physical and virtual memory facts."""
    modules = _entries(ps, "memoryModules")
    page_files = _entries(ps, "pageFiles")
    total = available = used = None
    load = None
    commit_limit = commit_available = None
    if memory:
        total = memory.get("totalPhysical")
        available = memory.get("availablePhysical")
        load = memory.get("loadPercent")
        commit_limit = memory.get("commitLimit")
        commit_available = memory.get("commitAvailable")
        if isinstance(total, int) and isinstance(available, int):
            used = total - available
    page_total = sum(item for item in (_int_or_none(pf.get("AllocatedBaseSize")) for pf in page_files) if item) or None
    page_used = sum(item for item in (_int_or_none(pf.get("CurrentUsage")) for pf in page_files) if item) or None
    return {
        "totalPhysical": format_bytes(total),
        "usedPhysical": format_bytes(used),
        "availablePhysical": format_bytes(available),
        "usagePercent": format_percent(load),
        "totalVirtual": format_bytes(commit_limit),
        "availableVirtual": format_bytes(commit_available),
        "pageFileSize": format_megabytes(page_total) if page_total else UNAVAILABLE,
        "pageFileUsage": format_megabytes(page_used) if page_used else UNAVAILABLE,
        "modules": _memory_modules(modules),
        "moduleCount": str(len(modules)) if modules else UNAVAILABLE,
    }


_SMBIOS_MEMORY_TYPES = {
    20: "DDR", 21: "DDR2", 22: "DDR2 FB-DIMM", 24: "DDR3", 26: "DDR4",
    27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5",
}


def _memory_modules(modules: list) -> list[dict]:
    """Render installed memory modules without guessing missing fields."""
    rendered = []
    for module in modules:
        capacity = _int_or_none(module.get("Capacity"))
        memory_type = _int_or_none(module.get("SMBIOSMemoryType"))
        rendered.append({
            "locator": format_text(module.get("DeviceLocator") or module.get("BankLabel")),
            "capacity": format_bytes(capacity),
            "type": _SMBIOS_MEMORY_TYPES.get(memory_type, UNAVAILABLE),
            "speed": f"{_int_or_none(module.get('Speed'))} MT/s" if _int_or_none(module.get("Speed")) else UNAVAILABLE,
            "manufacturer": format_text(module.get("Manufacturer")),
            "partNumber": format_text(module.get("PartNumber")),
        })
    return rendered


def build_graphics_section(ps: dict) -> list[dict]:
    """Collect one entry per graphics adapter, including displays."""
    rendered = []
    for gpu in _entries(ps, "gpus"):
        width = _int_or_none(gpu.get("CurrentHorizontalResolution"))
        height = _int_or_none(gpu.get("CurrentVerticalResolution"))
        refresh = _int_or_none(gpu.get("CurrentRefreshRate"))
        memory = _int_or_none(gpu.get("AdapterRAM"))
        rendered.append({
            "name": format_text(gpu.get("Name")),
            "manufacturer": format_text(gpu.get("AdapterCompatibility")),
            "driverVersion": format_text(gpu.get("DriverVersion")),
            "driverDate": format_moment(gpu.get("DriverDate")),
            "dedicatedMemory": format_bytes(memory) if memory else UNAVAILABLE,
            "sharedMemory": NOT_SUPPORTED,
            "utilization": NOT_SUPPORTED,
            "temperature": NOT_SUPPORTED,
            "resolution": f"{width} x {height}" if width and height else UNAVAILABLE,
            "refreshRate": f"{refresh} Hz" if refresh else UNAVAILABLE,
            "hdr": NOT_SUPPORTED,
            "status": format_text(gpu.get("Status")),
        })
    return rendered


def build_board_section(local: dict, ps: dict) -> dict:
    """Collect system, motherboard, BIOS, and platform-security facts."""
    board = _entry(ps, "board") or {}
    bios = _entry(ps, "bios") or {}
    tpm = _entry(ps, "tpm") or {}
    computer = _entry(ps, "computer") or {}
    return {
        "systemManufacturer": _pick(computer.get("Manufacturer"), local.get("systemManufacturer")),
        "systemModel": _pick(computer.get("Model"), local.get("systemProduct")),
        "systemType": format_text(computer.get("SystemType")),
        "boardManufacturer": _pick(board.get("Manufacturer"), local.get("baseBoardManufacturer")),
        "boardModel": _pick(board.get("Product"), local.get("baseBoardProduct")),
        "boardVersion": format_text(board.get("Version")),
        "biosManufacturer": _pick(bios.get("Manufacturer"), local.get("biosVendor")),
        "biosVersion": _pick(bios.get("SMBIOSBIOSVersion"), local.get("biosVersion")),
        "biosReleaseDate": _pick(bios.get("ReleaseDate"), local.get("biosReleaseDate")),
        "biosMode": local.get("firmwareType", UNAVAILABLE),
        "secureBoot": format_flag(ps.get("secureBoot")) if "secureBoot" in ps else UNAVAILABLE,
        "tpmPresent": format_flag(tpm.get("IsEnabled_InitialValue")) if tpm else UNAVAILABLE,
        "tpmVersion": format_text(tpm.get("SpecVersion")),
        "tpmManufacturerVersion": format_text(tpm.get("ManufacturerVersion")),
    }


def build_storage_section(ps: dict) -> dict:
    """Collect logical volumes, physical disks, health, and BitLocker state."""
    logical = []
    for drive in _entries(ps, "logicalDisks"):
        size = _int_or_none(drive.get("Size"))
        free = _int_or_none(drive.get("FreeSpace"))
        used = size - free if isinstance(size, int) and isinstance(free, int) else None
        percent = (used / size * 100.0) if isinstance(used, int) and size else None
        drive_type = _int_or_none(drive.get("DriveType"))
        logical.append({
            "device": format_text(drive.get("DeviceID")),
            "type": _DRIVE_TYPES.get(drive_type, UNAVAILABLE),
            "driveType": drive_type,
            "fileSystem": format_text(drive.get("FileSystem")),
            "label": format_text(drive.get("FileSystemLabel")),
            "total": format_bytes(size),
            "used": format_bytes(used),
            "free": format_bytes(free),
            "usedPercent": format_percent(percent) if percent is not None else UNAVAILABLE,
            # Kept as a number as well as a display string so the Overview
            # can classify a drive as High or Critical without re-parsing text.
            "percentValue": percent,
            "fixed": drive_type == _FIXED_DRIVE_TYPE,
        })
    health = {str(item.get("FriendlyName", "")): item for item in _entries(ps, "diskHealth")}
    physical = []
    for disk in _entries(ps, "physicalDisks"):
        name = str(disk.get("FriendlyName") or "")
        counters = health.get(name, {})
        temperature = _int_or_none(counters.get("Temperature"))
        physical.append({
            "model": format_text(disk.get("FriendlyName") or disk.get("Model")),
            "mediaType": format_text(disk.get("MediaType")),
            "busType": format_text(disk.get("BusType")),
            "size": format_bytes(_int_or_none(disk.get("Size"))),
            "interface": format_text(disk.get("BusType")),
            "health": format_text(disk.get("HealthStatus")),
            "operational": format_text(disk.get("OperationalStatus")),
            "temperature": f"{temperature} C" if temperature else UNAVAILABLE,
            "wear": f"{_int_or_none(counters.get('Wear'))}%" if _int_or_none(counters.get("Wear")) is not None else UNAVAILABLE,
        })
    bitlocker = []
    for volume in _entries(ps, "bitlocker"):
        percent = _int_or_none(volume.get("EncryptionPercentage"))
        bitlocker.append({
            "mountPoint": format_text(volume.get("MountPoint")),
            "status": _enum(volume.get("VolumeStatus"), _BITLOCKER_VOLUME_STATUS),
            "protection": _enum(volume.get("ProtectionStatus"), _BITLOCKER_PROTECTION),
            "encryption": _enum(volume.get("EncryptionMethod"), _BITLOCKER_METHOD),
            "percent": f"{percent}%" if percent is not None else UNAVAILABLE,
            "protected": _int_or_none(volume.get("ProtectionStatus")) == 1,
        })
    return {
        "logical": logical,
        "physical": physical,
        "bitlocker": bitlocker,
    }


# DriveType 3 is a fixed local disk. The Overview summarizes fixed volumes
# one by one and groups removable, network, and optical volumes into a single
# line; the Storage message lists the full detail for every volume.
_FIXED_DRIVE_TYPE = 3


_BITLOCKER_VOLUME_STATUS = {
    0: "Fully decrypted", 1: "Fully encrypted", 2: "Encryption in progress",
    3: "Decryption in progress", 4: "Encryption paused", 5: "Decryption paused",
}
_BITLOCKER_PROTECTION = {0: "Off", 1: "On", 2: "Unknown"}
_BITLOCKER_METHOD = {
    0: "None", 1: "AES-128 with diffuser", 2: "AES-256 with diffuser", 3: "AES-128",
    4: "AES-256", 5: "Hardware encryption", 6: "XTS-AES-128", 7: "XTS-AES-256",
}


def _enum(value, mapping: dict) -> str:
    """Render a numeric Windows enum through a mapping, else Unavailable."""
    number = _int_or_none(value)
    if number is None:
        return UNAVAILABLE
    return mapping.get(number, f"Unknown ({number})")


def build_network_section(ps: dict, public_ip: str | None, connectivity: bool | None,
                          agent: dict | None) -> dict:
    """Collect one entry per network adapter plus the connection summary."""
    addresses: dict[int, dict] = {}
    for entry in _entries(ps, "ipAddresses"):
        index = _int_or_none(entry.get("InterfaceIndex"))
        if index is None:
            continue
        bucket = addresses.setdefault(index, {"ipv4": [], "ipv6": []})
        address = entry.get("IPAddress")
        if not isinstance(address, str) or address.strip() == "":
            continue
        if _int_or_none(entry.get("AddressFamily")) == 2:
            bucket["ipv4"].append(f"{address}/{_int_or_none(entry.get('PrefixLength')) or 0}")
        else:
            bucket["ipv6"].append(address.strip())
    gateways: dict[int, str] = {}
    for entry in _entries(ps, "routes"):
        index = _int_or_none(entry.get("InterfaceIndex"))
        next_hop = entry.get("NextHop")
        if index is not None and isinstance(next_hop, str) and next_hop.strip():
            gateways.setdefault(index, next_hop.strip())
    dns: dict[int, list] = {}
    for entry in _entries(ps, "dns"):
        index = _int_or_none(entry.get("InterfaceIndex"))
        if index is None:
            continue
        servers = entry.get("ServerAddresses")
        if isinstance(servers, list):
            merged = dns.setdefault(index, [])
            for server in servers:
                if isinstance(server, str) and server.strip() and server.strip() not in merged:
                    merged.append(server.strip())
    dhcp: dict[int, int] = {}
    for entry in _entries(ps, "ipInterfaces"):
        index = _int_or_none(entry.get("InterfaceIndex"))
        value = _int_or_none(entry.get("Dhcp"))
        if index is not None and value is not None:
            dhcp[index] = max(dhcp.get(index, 0), value)
    stats = {str(item.get("Name", "")): item for item in _entries(ps, "adapterStats")}
    configs = _entries(ps, "adapterConfig")
    rendered = []
    adapters = _entries(ps, "adapters")
    for adapter in adapters:
        index = _int_or_none(adapter.get("ifIndex"))
        name = str(adapter.get("Name") or "")
        bucket = addresses.get(index or -1, {"ipv4": [], "ipv6": []})
        matching = [cfg for cfg in configs if _int_or_none(cfg.get("Index")) == index] or []
        if not matching:
            matching = [cfg for cfg in configs if str(cfg.get("Description") or "") == str(adapter.get("InterfaceDescription") or "")]
        config = matching[0] if matching else {}
        server = config.get("DHCPServer")
        counter = stats.get(name, {})
        rendered.append({
            "name": format_text(name),
            "description": format_text(adapter.get("InterfaceDescription")),
            "manufacturer": NOT_SUPPORTED,
            "status": format_text(adapter.get("Status")),
            "virtual": format_flag(adapter.get("Virtual")),
            "ipv4": ", ".join(bucket["ipv4"]) if bucket["ipv4"] else UNAVAILABLE,
            "ipv6": ", ".join(bucket["ipv6"]) if bucket["ipv6"] else "None",
            "subnet": _subnet(config),
            "gateway": format_text(gateways.get(index or -1) or _first(config.get("DefaultIPGateway"))),
            "dns": ", ".join(dns.get(index or -1, [])) if dns.get(index or -1) else UNAVAILABLE,
            "dhcpEnabled": _dhcp_flag(config, dhcp.get(index or -1)),
            "dhcpServer": format_text(server),
            "mac": format_text(adapter.get("MacAddress")),
            "linkSpeed": format_text(adapter.get("LinkSpeed")),
            "received": format_bytes(_int_or_none(counter.get("ReceivedBytes"))) if counter else UNAVAILABLE,
            "sent": format_bytes(_int_or_none(counter.get("SentBytes"))) if counter else UNAVAILABLE,
            "up": str(adapter.get("Status") or "").lower() == "up",
        })
    rendered.sort(key=lambda item: (not item["up"], item["name"]))
    facts = agent or {}
    connected = facts.get("connected")
    internet = None
    if connectivity is True:
        internet = "Reachable" if public_ip else "Reachable (public address lookup failed)"
    elif connectivity is False:
        internet = "Unreachable"
    return {
        "adapters": rendered,
        "internetConnectivity": format_text(internet),
        "discordConnection": "Connected" if connected else UNAVAILABLE,
        "discordLatency": _latency(facts.get("latencySeconds")),
        "publicIpv4": format_text(public_ip),
    }


def _latency(value) -> str:
    """Render a gateway latency in seconds as milliseconds, or Unavailable."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return UNAVAILABLE
    if seconds < 0:
        return UNAVAILABLE
    return f"{seconds * 1000:.0f} ms"


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _subnet(config: dict) -> str:
    """Render the IPv4 subnet mask from the adapter configuration."""
    masks = config.get("IPSubnet")
    if isinstance(masks, list):
        for mask in masks:
            if isinstance(mask, str) and _is_ipv4(mask):
                return mask
    return UNAVAILABLE


def _dhcp_flag(config: dict, wmi_flag) -> str:
    """Render DHCP enablement, preferring the WMI adapter configuration."""
    value = config.get("DHCPEnabled")
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if wmi_flag is not None:
        return "Yes" if wmi_flag else "No"
    return UNAVAILABLE


def build_power_section(ps: dict, power: dict | None) -> dict:
    """Collect power source, battery, and active power plan facts."""
    battery = _entry(ps, "battery") or {}
    plan = _entry(ps, "powerPlan") or {}
    percent = _int_or_none(battery.get("EstimatedChargeRemaining"))
    if percent is None and power and power.get("batteryPercent") not in (None, 255):
        percent = power.get("batteryPercent")
    ac_line = power.get("acLine") if power else None
    if ac_line == 1:
        source = "AC power"
    elif ac_line == 0:
        source = "Battery"
    else:
        source = UNAVAILABLE
    present = bool(battery) or (power is not None and power.get("batteryFlag") not in (128, 255, None))
    return {
        "powerSource": source,
        "batteryPresent": NOT_APPLICABLE if not present else "Yes",
        "batteryLevel": f"{percent}%" if percent is not None else (NOT_APPLICABLE if not present else UNAVAILABLE),
        "charging": _battery_state(battery.get("BatteryStatus"), ac_line) if present else NOT_APPLICABLE,
        "powerPlan": format_text(plan.get("ElementName")),
        "sleepStates": NOT_SUPPORTED,
    }


_BATTERY_STATUS = {
    1: "Discharging", 2: "On AC power", 3: "Fully charged", 4: "Low", 5: "Critical",
    6: "Charging", 7: "Charging and high", 8: "Charging and low", 9: "Charging and critical",
    10: "Undefined", 11: "Partially charged",
}


def _battery_state(status, ac_line) -> str:
    value = _int_or_none(status)
    if value is not None and value in _BATTERY_STATUS:
        return _BATTERY_STATUS[value]
    if ac_line == 1:
        return "On AC power"
    if ac_line == 0:
        return "Discharging"
    return UNAVAILABLE


def build_security_section(local: dict, ps: dict, board: dict, storage: dict) -> dict:
    """Collect Defender, firewall, update, and platform-security facts."""
    defender = _entry(ps, "defender") or {}
    firewall = _entries(ps, "firewall")
    hotfixes = _entries(ps, "hotfixes")
    newest = None
    for entry in hotfixes:
        stamp = str(entry.get("InstalledOn") or "")
        if stamp and (newest is None or stamp > newest[0]):
            newest = (stamp, entry)
    firewall_profiles = [
        {"profile": format_text(item.get("Name")), "state": _flag_text(item.get("Enabled"))}
        for item in firewall
    ]
    protection = storage.get("bitlocker") or []
    protected = [item for item in protection if item.get("protected")]
    return {
        "defenderStatus": _defender_status(defender),
        "realTimeProtection": format_flag(defender.get("RealTimeProtectionEnabled")) if defender else UNAVAILABLE,
        "defenderSignature": format_text(defender.get("AntivirusSignatureVersion")),
        "defenderSignatureDate": format_moment(defender.get("AntivirusSignatureLastUpdated")),
        "firewallProfiles": firewall_profiles,
        "lastUpdate": f"{newest[1].get('HotFixID')} ({format_moment(newest[0])})" if newest else UNAVAILABLE,
        "hotfixCount": str(len(hotfixes)) if hotfixes else UNAVAILABLE,
        "pendingUpdates": NOT_SUPPORTED,
        "uacStatus": _uac_summary(local),
        "secureBoot": board.get("secureBoot", UNAVAILABLE),
        "tpmStatus": _tpm_summary(board),
        "bitlockerStatus": (
            f"{len(protected)}/{len(protection)} volumes protected" if protection else UNAVAILABLE
        ),
    }


def _flag_text(value) -> str:
    """Render a tri-state Windows flag as On/Off, or Unavailable when unknown.

    PowerShell serializes the same CIM boolean as `true`/`false` in some
    providers and as 1/0 in others, so both shapes are accepted and a
    genuinely absent value stays Unavailable instead of silently Off.
    """
    if isinstance(value, bool):
        return "On" if value else "Off"
    number = _int_or_none(value)
    if number is not None:
        return "On" if number != 0 else "Off"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "enabled"):
            return "On"
        if lowered in ("false", "no", "off", "disabled"):
            return "Off"
    return UNAVAILABLE


def _defender_status(defender: dict) -> str:
    if not defender:
        return UNAVAILABLE
    enabled = defender.get("AntivirusEnabled")
    realtime = defender.get("RealTimeProtectionEnabled")
    if enabled is None and realtime is None:
        return UNAVAILABLE
    return f"{'Enabled' if enabled else 'Disabled'} (real-time {'on' if realtime else 'off'})" if enabled is not None else UNAVAILABLE


def _uac_summary(local: dict) -> str:
    enabled = local.get("uacEnabled", UNAVAILABLE)
    prompt = local.get("uacPrompt", UNAVAILABLE)
    if enabled == UNAVAILABLE and prompt == UNAVAILABLE:
        return UNAVAILABLE
    return f"{enabled} (prompt: {prompt})"


def _tpm_summary(board: dict) -> str:
    present = board.get("tpmPresent", UNAVAILABLE)
    version = board.get("tpmVersion", UNAVAILABLE)
    if present == UNAVAILABLE and version == UNAVAILABLE:
        return UNAVAILABLE
    return f"{present} (spec {version})" if version != UNAVAILABLE else present


def build_performance_section(ps: dict, cpu: float | None, memory: dict | None, window_ms: int | None,
                              now=None) -> dict:
    """Collect instantaneous performance counters and the sampling window."""
    disk_read = disk_write = disk_time = None
    for entry in _entries(ps, "diskPerf"):
        name = str(entry.get("Name") or "")
        if not name.lower().startswith("_total"):
            continue
        disk_read = _int_or_none(entry.get("DiskReadBytesPerSec"))
        disk_write = _int_or_none(entry.get("DiskWriteBytesPerSec"))
        disk_time = _int_or_none(entry.get("PercentDiskTime"))
    net_received = net_sent = 0
    for entry in _entries(ps, "netPerf"):
        value = _int_or_none(entry.get("BytesReceivedPersec"))
        if value:
            net_received += value
        value = _int_or_none(entry.get("BytesSentPersec"))
        if value:
            net_sent += value
    return {
        "cpuUtilization": format_percent(cpu) if cpu is not None else UNAVAILABLE,
        "memoryUtilization": format_percent(memory.get("loadPercent")) if memory else UNAVAILABLE,
        "diskRead": f"{format_bytes(disk_read)}/s" if disk_read is not None else UNAVAILABLE,
        "diskWrite": f"{format_bytes(disk_write)}/s" if disk_write is not None else UNAVAILABLE,
        "diskBusy": format_percent(disk_time) if disk_time is not None else UNAVAILABLE,
        "networkReceived": f"{format_bytes(net_received)}/s" if net_received else UNAVAILABLE,
        "networkSent": f"{format_bytes(net_sent)}/s" if net_sent else UNAVAILABLE,
        "sampleWindow": f"{window_ms} ms" if window_ms else UNAVAILABLE,
        "sampleTimestamp": _now(now).strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_environment_section(state_directory, log_path) -> dict:
    """Collect agent state-directory availability facts.

    Only directory availability is reported; environment variable values are
    never echoed, so no secrets or private paths can leak. The Windows and
    system directories are collected once, by the OS section, and are not
    repeated here.
    """
    return {
        "environmentStatus": "Collected (values withheld)",
        "stateDirectory": directory_availability(state_directory),
        "diagnosticLog": directory_availability(log_path),
    }


# ---------------------------------------------------------------------------
# Agent facts, directory availability, and the collection entry point
# ---------------------------------------------------------------------------


def directory_availability(path) -> str:
    """Report whether a directory or file exists, without creating anything."""
    if not path:
        return UNAVAILABLE
    try:
        return "Available" if Path(path).exists() else "Not created yet"
    except OSError:
        return UNAVAILABLE


def _state_paths(env=None) -> tuple[str | None, str | None]:
    """Return the state directory and diagnostic log path for reporting."""
    from astral_remote.agent.error_log import LOG_FILENAME, log_dir

    directory = log_dir(env)
    if directory is None:
        return None, None
    return str(directory), str(directory / LOG_FILENAME)


def _agent_facts(agent: dict | None, env=None) -> dict:
    """Merge caller-supplied agent facts with read-only local probes.

    Bot-side facts (installation ID, connection state, latency, managed
    channel counts) come from the caller. Startup registration and the
    diagnostic-log summary are read-only local probes, used only when the
    caller did not already supply them.
    """
    from astral_remote import AGENT_VERSION

    facts = dict(agent or {})
    facts.setdefault("agentVersion", str(AGENT_VERSION))
    if not isinstance(facts.get("registration"), dict):
        from astral_remote.agent.startup import read_registration

        facts["registration"] = read_registration(facts.get("startupName"), env)
    if not isinstance(facts.get("logSummary"), dict):
        from astral_remote.agent.error_log import summarize_log

        facts["logSummary"] = summarize_log(env)
    return facts


def _registration_text(registration: dict) -> str:
    """Render Run-key and logon-task registration state."""
    run_key = format_text(registration.get("runKey"))
    task = format_text(registration.get("logonTask"))
    if run_key == UNAVAILABLE and task == UNAVAILABLE:
        return UNAVAILABLE
    return f"Run key: {run_key} | Logon task: {task}"


def _backup_text(registration: dict) -> str:
    """Render backup-copy availability without exposing a local path."""
    present = registration.get("backupPresent")
    if present is None:
        return UNAVAILABLE
    return "Present" if present else "Missing"


def _log_text(summary: dict) -> str:
    """Render the diagnostic-log summary; counts only, never message text."""
    if not summary.get("present"):
        return "No diagnostic entries"
    return (
        f"{summary.get('entries', 0)} entries "
        f"({summary.get('warnings', 0)} warnings, {summary.get('fatals', 0)} fatal); "
        f"last {format_text(summary.get('lastTimestamp'))}"
    )


def build_identity_section(local: dict, ps: dict, facts: dict, public_ip, now) -> dict:
    """Collect the PC identification summary used by the report header.

    The detailed hardware facts live in their own sections; this dict
    only carries what the header shows, so nothing is duplicated.
    """
    os_info = _entry(ps, "os") or {}
    return {
        "hostname": format_text(facts.get("hostname")),
        "installationId": format_text(facts.get("installationId")),
        "localIpv4": format_text(facts.get("localIpv4")),
        "publicIpv4": format_text(public_ip),
        "osName": _pick(os_info.get("Caption"), local.get("osName")),
        "osVersion": _os_version_text(os_info, local),
        "agentStatus": "ONLINE" if facts.get("connected") else "UNKNOWN",
        "agentVersion": format_text(facts.get("agentVersion")),
        "lastUpdated": _now(now).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _os_version_text(os_info: dict, local: dict) -> str:
    """Compose `version (build ...)` from WMI and registry facts."""
    version = format_text(os_info.get("Version"))
    build = _pick(local.get("osBuild"), os_info.get("BuildNumber"))
    if version == UNAVAILABLE:
        version = local.get("osVersion", UNAVAILABLE)
    if version == UNAVAILABLE and build == UNAVAILABLE:
        return UNAVAILABLE
    if build == UNAVAILABLE:
        return version
    return f"{version} (build {build})"


def build_agent_section(facts: dict) -> dict:
    """Collect Astral Remote agent facts, including read-only install state."""
    registration = facts.get("registration") or {}
    return {
        "version": format_text(facts.get("agentVersion")),
        "installationId": format_text(facts.get("installationId")),
        "uptime": format_duration(facts.get("uptimeSeconds")),
        "connection": format_text(
            facts.get("connection") or ("Connected" if facts.get("connected") else None)
        ),
        "latency": _latency(facts.get("latencySeconds")),
        "reconnectCount": (
            str(facts.get("reconnects")) if isinstance(facts.get("reconnects"), int) else UNAVAILABLE
        ),
        "lastUpdate": NOT_SUPPORTED,
        # The only operator command is the read-only `status` in the managed
        # #main channel, so no management permission is required to use it;
        # the report states that instead of naming a permission.
        "requiredPermission": f"{NOT_APPLICABLE} (status needs no management permission)",
        "categoryStatus": format_text(facts.get("categoryStatus")),
        "channelCount": (
            str(facts.get("channelCount")) if isinstance(facts.get("channelCount"), int) else UNAVAILABLE
        ),
        "duplicateWarning": format_text(facts.get("duplicateWarning") or "None detected"),
        "startupRegistration": _registration_text(registration),
        "backupExecutable": _backup_text(registration),
        "logSummary": _log_text(facts.get("logSummary") or {}),
    }


def collect(agent: dict | None = None, *, ps_data=None, runner=None, env=None, now=None) -> dict:
    """Collect the complete read-only system inventory for the dashboard.

    Every probe is guarded, so a partial failure degrades only its own
    fields to Unavailable and never aborts the collection. `ps_data` and
    `runner` let tests inject a fixed PowerShell reply; when omitted the
    single CIM pass runs here. This function blocks on subprocesses and
    one short CPU sample, so callers run it on a worker thread.
    """
    moment = _now(now)
    local = _local_facts(env=env, now=moment)
    ps = ps_data if isinstance(ps_data, dict) else run_powershell_json(_PS_INVENTORY, runner=runner)
    memory = read_memory_status()
    power = read_power_status()
    utilization = read_cpu_utilization()
    public_ip = read_public_ipv4()
    connectivity = _reachable() if public_ip is None else True
    facts = _agent_facts(agent, env)
    board = build_board_section(local, ps)
    storage = build_storage_section(ps)
    os_section = build_os_section(local, ps)
    state_directory, log_path = _state_paths(env)
    return {
        "identity": build_identity_section(local, ps, facts, public_ip, moment),
        "os": os_section,
        "cpu": build_cpu_section(local, ps, utilization),
        "memory": build_memory_section(ps, memory),
        "graphics": build_graphics_section(ps),
        "board": board,
        "storage": storage,
        "network": build_network_section(ps, public_ip, connectivity, facts),
        "power": build_power_section(ps, power),
        "security": build_security_section(local, ps, board, storage),
        "performance": build_performance_section(
            ps, utilization, memory, int(CPU_SAMPLE_SECONDS * 1000), moment
        ),
        "environment": build_environment_section(state_directory, log_path),
        "agent": build_agent_section(facts),
    }
