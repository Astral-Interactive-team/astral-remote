# Components V2 rendering and publication for the managed #info dashboard.
# The collector in windows/inventory.py returns display-ready facts; this
# module is the presentation half. It turns one inventory into a fixed,
# ordered set of message groups ("Overview", "Windows & Operating System",
# ...), packs those groups into Discord messages, and republishes them into
# the existing per-computer #info channel.
# Visual hierarchy, largest to smallest:
#   #  Astral Remote — System Information (Part N of M)   message title
#   ## <group>                                          message topic
#   ### <section>                                       topic section
#   **Label:** value / **Item n: name** / - bullet       detail lines
#   > `value`                                           highlighted identity
# The Overview opens with a connection banner before its first section heading,
# so the live gateway state is the most prominent fact in the report. IP
# addresses and other identifiers use inline code; only identity-worthy values
# such as the IPs are also quoted.
# Discord exposes only three heading levels, so individual GPUs, memory
# modules, and network adapters use bold item headers instead of a fourth
# level. Every message always opens with its title and its group heading, so
# a reader can identify a message without scrolling.
# A group always starts its own message; a group that outgrows one message
# continues in additional messages, where the same group heading is printed
# again without any continuation marker. No select menu, button, or other
# interactive control is ever added.
# Message limits (4000 display characters and 40 components per message) are
# enforced before every send, and a failing part is logged instead of being
# recorded as published. No embeds are used: every title, body, and divider
# is a V2 component.

from __future__ import annotations

from dataclasses import dataclass

from astral_remote.agent.windows.inventory import NOT_SUPPORTED, UNAVAILABLE
from astral_remote.shared.logging_utils import warn

TITLE = "Astral Remote — System Information"
ACCENT_COLOUR = 0x6C5CE7

# Message group order. The first group is always the Overview dashboard and
# every later group is one topical message.
GROUP_OVERVIEW = "Overview"
GROUP_WINDOWS = "Windows & Operating System"
GROUP_COMPUTE = "Processor & Memory"
GROUP_GRAPHICS = "Graphics & Firmware"
GROUP_STORAGE = "Storage"
GROUP_NETWORK = "Network & Power"
GROUP_SECURITY = "Security & Updates"
GROUP_AGENT = "Performance & Agent"
GROUPS = (
    GROUP_OVERVIEW,
    GROUP_WINDOWS,
    GROUP_COMPUTE,
    GROUP_GRAPHICS,
    GROUP_STORAGE,
    GROUP_NETWORK,
    GROUP_SECURITY,
    GROUP_AGENT,
)

# Utilization thresholds. A metric at or above the warning threshold is
# annotated with text and raises the container accent colour, so the same
# fact is carried by colour and by words; nothing is ever signalled by
# colour alone.
WARN_PERCENT = 80.0
CRITICAL_PERCENT = 90.0
_LEVEL_RANK = {None: 0, "warning": 1, "critical": 2}
LEVEL_COLOURS = {"warning": 0xE67E22, "critical": 0xE74C3C}
LEVEL_ICONS = {
    "healthy": "\U0001F7E2",
    "warning": "\U0001F7E1",
    "critical": "\U0001F534",
}

# Connection vocabulary. The icon always accompanies the word, so a colour
# alone never carries the state and an unconfirmed state is never rendered as
# Connected.
CONNECTION_ICONS = {
    "Connected": "\U0001F7E2",
    "Disconnected": "\U0001F534",
    "Unknown": "\U0001F7E1",
}
_CONNECTION_ALIASES = {
    "online": "Connected",
    "offline": "Disconnected",
    "unknown": "Unknown",
    "unavailable": "Unknown",
}
_HEALTH_ICONS = {
    "healthy": "\U0001F7E2",
    "warning": "\U0001F7E1",
    "degraded": "\U0001F7E1",
    "unhealthy": "\U0001F534",
    "critical": "\U0001F534",
}

# Discord limits: 4000 display characters and 40 components per message. The
# container itself counts as one component, and the published budgets leave a
# safety margin so a late change can never push a real message over the limit.
MAX_TEXT_LENGTH = 4000
MAX_COMPONENTS = 40
TEXT_BUDGET = 3800
COMPONENT_BUDGET = MAX_COMPONENTS - 1

# Longest body text one TextDisplay holds before the section is chunked.
SECTION_CHUNK = 1700

# Room held back on every page for the message title and the group heading.
# Both are added at view-build time, after packing.
PAGE_RESERVE_TEXT = 160
PAGE_RESERVE_COMPONENTS = 4


@dataclass(frozen=True)
class Block:
    """One TextDisplay of the report.

    `group` names the message group this block belongs to, so packing can
    start a new message at every group boundary, and every message can print
    its topic heading. `heading` marks a block that opens a section,
    `continuation` marks a body that carries on a section started earlier, and
    `section` names that section. Continuation is never re-labelled when it is
    rendered: the message title already carries the position. `level` marks a
    block that carries an attention-worthy measurement.
    """

    text: str
    group: str = ""
    heading: bool = False
    continuation: bool = False
    section: str | None = None
    level: str | None = None


@dataclass(frozen=True, eq=False)
class Topic:
    """One report section: a group, a section title, and its display lines.

    `heading` is False only for a preamble that belongs directly under the
    group heading, such as the Overview connection banner.
    """

    group: str
    title: str
    lines: list[str]
    level: str | None = None
    heading: bool = True


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def _field(label: str, value) -> str:
    """Render one `**Label:** value` line, defaulting to Unavailable."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = UNAVAILABLE
    return f"**{label}:** {value}"


def _plain(value) -> str:
    """Render a value inside a bold item header without nested markdown."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return UNAVAILABLE
    return str(value)


def _code(value) -> str:
    """Inline-code an identifier, leaving explicit absence markers plain."""
    text = _plain(value)
    if text in (UNAVAILABLE, NOT_SUPPORTED):
        return text
    return f"`{text}`"


def _code_field(label: str, value) -> str:
    """Render a label with an identifier-like value in inline code."""
    return _field(label, _code(value))


def _quote_field(label: str, value) -> str:
    """Render a label followed by a quoted value, used for IP addresses."""
    text = _plain(value)
    quoted = f"> {text}" if text in (UNAVAILABLE, NOT_SUPPORTED) else f"> `{text}`"
    return f"**{label}**\n{quoted}"


def _connection(value) -> str:
    """Render a gateway state with its status icon, mapping legacy words."""
    text = _plain(value)
    text = _CONNECTION_ALIASES.get(text.lower(), text)
    icon = CONNECTION_ICONS.get(text)
    return f"{icon} {text}" if icon else text


def _connection_badge(value) -> str:
    """Render the Overview connection banner, never inventing a connection."""
    text = _plain(value)
    text = _CONNECTION_ALIASES.get(text.lower(), text)
    if text not in CONNECTION_ICONS:
        text = "Unknown"
    return f"{CONNECTION_ICONS[text]} **Connection: {text}**"


def _health(value) -> str:
    """Prefix a reported disk health state with a matching status icon."""
    text = _plain(value)
    icon = _HEALTH_ICONS.get(text.lower())
    return f"{icon} {text}" if icon else text


def _number(text):
    """Parse a display percentage such as `45.0%` back into a float."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    try:
        return float(text.strip().rstrip("%").strip())
    except ValueError:
        return None


def _usage_level(percent):
    """Classify a utilization percentage into None, warning, or critical."""
    if percent is None:
        return None
    if percent >= CRITICAL_PERCENT:
        return "critical"
    if percent >= WARN_PERCENT:
        return "warning"
    return None


def _worst_level(*levels):
    """Return the highest severity among the given levels."""
    return max(levels, key=lambda level: _LEVEL_RANK.get(level, 0))


def _level_suffix(percent) -> str:
    """Annotate a measured utilization with its status word and icon."""
    if percent is None:
        return ""
    level = _usage_level(percent)
    if level == "critical":
        return f" - {LEVEL_ICONS['critical']} **Critical**"
    if level == "warning":
        return f" - {LEVEL_ICONS['warning']} **High**"
    return f" - {LEVEL_ICONS['healthy']} **Healthy**"


def _memory_usage_text(memory: dict) -> str:
    """Compose the memory summary as `45.0% (8.0 GB of 16.0 GB)`."""
    percent = memory.get("usagePercent")
    used = memory.get("usedPhysical")
    total = memory.get("totalPhysical")
    if percent is None and used is None:
        return UNAVAILABLE
    if percent is None:
        return f"{used} of {total}" if used is not None and total is not None else UNAVAILABLE
    if used is not None and total is not None:
        return f"{percent} ({used} of {total})"
    return str(percent)


def _inferred(value) -> str:
    """Mark a locally inferred fact so it is not read as a measured one."""
    text = _plain(value)
    return text if text == UNAVAILABLE else f"{text} (inferred)"


def _device(value) -> str:
    """Render a drive letter or mount point without a trailing separator."""
    text = _plain(value)
    if text == UNAVAILABLE:
        return text
    return text.strip().rstrip("\\/")


# ---------------------------------------------------------------------------
# Message 1 — Overview
# ---------------------------------------------------------------------------


def _overview_topics(inventory: dict) -> list[Topic]:
    """Build the Overview dashboard: connection, identity, and headline numbers."""
    identity = inventory.get("identity") or {}
    cpu = inventory.get("cpu") or {}
    memory = inventory.get("memory") or {}
    storage = inventory.get("storage") or {}

    cpu_percent = _number(cpu.get("utilization"))
    memory_percent = _number(memory.get("usagePercent"))
    storage_lines, storage_level = _overview_storage(storage)
    # `connection` is the current field; `agentStatus` keeps reports built by
    # older collectors readable without inventing a live state.
    connection = identity.get("connection") or identity.get("agentStatus")
    return [
        # No section heading: the banner sits directly under the Overview
        # heading, where it is the first and most prominent fact.
        Topic(GROUP_OVERVIEW, "Connection", [_connection_badge(connection)],
              heading=False),
        Topic(GROUP_OVERVIEW, "Machine", [
            _code_field("Hostname", identity.get("hostname")),
            _code_field("Installation ID", identity.get("installationId")),
        ]),
        Topic(GROUP_OVERVIEW, "Network Identity", [
            _quote_field("Local IPv4", identity.get("localIpv4")),
            "",
            _quote_field("Public IPv4", identity.get("publicIpv4")),
        ]),
        Topic(GROUP_OVERVIEW, "System Summary", [
            _field("Operating System", identity.get("osName")),
            _field("OS Version / Build", identity.get("osVersion")),
            _code_field("Agent Version", identity.get("agentVersion")),
            _field(
                "CPU Utilization",
                f"{_plain(cpu.get('utilization'))}{_level_suffix(cpu_percent)}",
            ),
            _field(
                "Memory Usage",
                f"{_memory_usage_text(memory)}{_level_suffix(memory_percent)}",
            ),
            _field("Last Updated", identity.get("lastUpdated")),
        ]),
        Topic(GROUP_OVERVIEW, "Storage", storage_lines,
              level=storage_level),
    ]


def _overview_storage(storage: dict) -> tuple[list[str], str | None]:
    """Summarize drive usage and disk health for the Overview message.

    Only the usage percentage, free space, and total size are shown here;
    capacity, file system, and BitLocker detail stay in the Storage message,
    so the summary and the detail never compete. Fixed volumes are listed
    one by one; removable, network, and optical volumes are grouped into a
    single line so they cannot crowd the dashboard.
    """
    logical = storage.get("logical") or []
    physical = storage.get("physical") or []
    fixed = [drive for drive in logical if drive.get("fixed")]
    if fixed:
        others = [drive for drive in logical if not drive.get("fixed")]
    else:
        fixed, others = list(logical), []

    lines: list[str] = []
    level = None
    for drive in fixed:
        percent = drive.get("percentValue")
        level = _worst_level(level, _usage_level(percent))
        lines.append(
            f"- **{_code(_device(drive.get('device')))}** {_plain(drive.get('usedPercent'))} used | "
            f"{_plain(drive.get('free'))} free of {_plain(drive.get('total'))}"
            f"{_level_suffix(percent)}"
        )
    if others:
        lines.append(
            "- **Other volumes:** "
            + ", ".join(
                f"{_code(_device(drive.get('device')))} ({_plain(drive.get('type'))})"
                for drive in others
            )
        )
    for disk in physical:
        lines.append(
            f"- **Physical disk:** {_plain(disk.get('model'))} | "
            f"health {_health(disk.get('health'))} | state {_plain(disk.get('operational'))}"
        )
    if not lines:
        lines.append(_field("Storage", UNAVAILABLE))
    return lines, level


# ---------------------------------------------------------------------------
# Message 2 — Windows & Operating System
# ---------------------------------------------------------------------------


def _windows_topics(os_info: dict) -> list[Topic]:
    """Build the operating-system group from the collected OS facts."""
    return [
        Topic(GROUP_WINDOWS, "Edition & Build", [
            _field("OS name", os_info.get("osName")),
            _field("OS manufacturer", os_info.get("osManufacturer")),
            _field("OS version", os_info.get("osVersion")),
            _field("OS build number", os_info.get("osBuild")),
            _field("Feature update", os_info.get("osDisplayVersion")),
            _field("Edition", os_info.get("osEdition")),
            _field("Architecture", os_info.get("osArchitecture")),
            _field("OS configuration", os_info.get("osConfiguration")),
            _field("OS build type", os_info.get("osBuildType")),
        ]),
        Topic(GROUP_WINDOWS, "Windows Registration", [
            _field("Registered owner", os_info.get("registeredOwner")),
            _field("Registered organization", os_info.get("registeredOrganization")),
            _code_field("Windows product ID", os_info.get("productId")),
            _field("Original install date", os_info.get("originalInstallDate")),
        ]),
        Topic(GROUP_WINDOWS, "Runtime", [
            _field("System boot time", os_info.get("bootTime")),
            _field("System uptime", os_info.get("uptime")),
            _field("System locale", os_info.get("systemLocale")),
            _field("Input locale", os_info.get("inputLocale")),
            _field("Time zone", os_info.get("timeZone")),
        ]),
        Topic(GROUP_WINDOWS, "Domain & Directories", [
            _field("Domain / workgroup", os_info.get("domainWorkgroup")),
            _field("Domain joined", os_info.get("partOfDomain")),
            _field("Windows directory", os_info.get("windowsDirectory")),
            _field("System directory", os_info.get("systemDirectory")),
            _field("Boot device", os_info.get("bootDevice")),
            _field("Firmware architecture", os_info.get("firmwareArchitecture")),
        ]),
    ]


# ---------------------------------------------------------------------------
# Message 3 — Processor & Memory
# ---------------------------------------------------------------------------


def _compute_topics(cpu: dict, memory: dict) -> list[Topic]:
    """Build the processor and memory group."""
    cpu_percent = _number(cpu.get("utilization"))
    memory_percent = _number(memory.get("usagePercent"))
    cpu_lines = [
        _field("CPU utilization", f"{_plain(cpu.get('utilization'))}{_level_suffix(cpu_percent)}"),
        _field("CPU temperature", cpu.get("temperature")),
        _field("Manufacturer", cpu.get("manufacturer")),
        _field("Model", cpu.get("model")),
        _code_field("Identifier", cpu.get("identifier")),
        _field("Architecture", cpu.get("architecture")),
        _field("Physical cores", cpu.get("physicalCores")),
        _field("Logical processors", cpu.get("logicalProcessors")),
        _field("Maximum clock speed", cpu.get("maxClock")),
        _field("Current clock speed", cpu.get("currentClock")),
        _field("L2 cache", cpu.get("l2Cache")),
        _field("L3 cache", cpu.get("l3Cache")),
        _field("Virtualization support (firmware)", cpu.get("virtualizationFirmware")),
        _field("Virtualization extensions (SLAT)", cpu.get("virtualizationExtensions")),
        _field("Hypervisor present", cpu.get("hypervisorPresent")),
    ]
    memory_lines = [
        _field(
            "Memory usage",
            f"{_memory_usage_text(memory)}{_level_suffix(memory_percent)}",
        ),
        _field("Total physical memory", memory.get("totalPhysical")),
        _field("Used memory", memory.get("usedPhysical")),
        _field("Available memory", memory.get("availablePhysical")),
        _field("Total virtual memory (commit limit)", memory.get("totalVirtual")),
        _field("Available virtual memory", memory.get("availableVirtual")),
        _field("Page file size", memory.get("pageFileSize")),
        _field("Page file usage", memory.get("pageFileUsage")),
        _field("Installed module count", memory.get("moduleCount")),
    ]
    modules = memory.get("modules") or []
    if modules:
        memory_lines.append("")
        memory_lines.append(f"**Installed modules ({len(modules)})**")
        memory_lines.extend(
            f"- {_plain(module.get('locator'))} | {_plain(module.get('capacity'))} | "
            f"{_plain(module.get('type'))} | {_plain(module.get('speed'))} | "
            f"{_plain(module.get('manufacturer'))} | {_plain(module.get('partNumber'))}"
            for module in modules
        )
    else:
        memory_lines.append(_field("Installed modules", UNAVAILABLE))

    return [
        Topic(GROUP_COMPUTE, "Processor", cpu_lines, level=_usage_level(cpu_percent)),
        Topic(GROUP_COMPUTE, "Memory", memory_lines, level=_usage_level(memory_percent)),
    ]


# ---------------------------------------------------------------------------
# Message 4 — Graphics & Firmware
# ---------------------------------------------------------------------------


def _graphics_topics(graphics: list, board: dict) -> list[Topic]:
    """Build the graphics adapters plus the motherboard and firmware group."""
    adapters: list[str] = []
    if not graphics:
        adapters.append(_field("Graphics adapters", UNAVAILABLE))
    for index, gpu in enumerate(graphics, start=1):
        if index > 1:
            adapters.append("")
        adapters.extend([
            f"**GPU {index}: {_plain(gpu.get('name'))}**",
            _field("Manufacturer", gpu.get("manufacturer")),
            _field("Driver version", gpu.get("driverVersion")),
            _field("Driver date", gpu.get("driverDate")),
            _field("Dedicated memory (reported)", gpu.get("dedicatedMemory")),
            _field("Resolution", gpu.get("resolution")),
            _field("Refresh rate", gpu.get("refreshRate")),
            _field("HDR", gpu.get("hdr")),
            _field("Adapter status", gpu.get("status")),
        ])
        # Windows exposes no per-adapter utilization, temperature, or shared
        # memory, so the unsupported meters are stated once and name each
        # capability instead of repeating the same marker on three lines.
        unsupported = [
            label for label, value in (
                ("utilization", gpu.get("utilization")),
                ("temperature", gpu.get("temperature")),
                ("shared memory", gpu.get("sharedMemory")),
            )
            if value == NOT_SUPPORTED
        ]
        if unsupported:
            adapters.append(
                _field("Monitoring", f"{NOT_SUPPORTED} ({', '.join(unsupported)})")
            )
    if graphics:
        adapters.append(_field("Detected adapters", str(len(graphics))))

    return [
        Topic(GROUP_GRAPHICS, "Graphics Adapters", adapters),
        Topic(GROUP_GRAPHICS, "System & Motherboard", [
            _field("System manufacturer", board.get("systemManufacturer")),
            _field("System model", board.get("systemModel")),
            _field("System type", board.get("systemType")),
            _field("Motherboard manufacturer", board.get("boardManufacturer")),
            _field("Motherboard model", board.get("boardModel")),
            _field("Motherboard version", board.get("boardVersion")),
        ]),
        Topic(GROUP_GRAPHICS, "BIOS & Firmware", [
            _field("BIOS manufacturer", board.get("biosManufacturer")),
            _field("BIOS version", board.get("biosVersion")),
            _field("BIOS release date", board.get("biosReleaseDate")),
            # The firmware mode is inferred from the Secure Boot state key,
            # never read from firmware directly, so it is labelled as inferred.
            _field("Firmware mode", _inferred(board.get("biosMode"))),
            _field("Secure Boot", board.get("secureBoot")),
        ]),
        Topic(GROUP_GRAPHICS, "Trusted Platform Module", [
            _field("TPM available", board.get("tpmPresent")),
            _field("TPM version", board.get("tpmVersion")),
            _field("TPM firmware", board.get("tpmManufacturerVersion")),
        ]),
    ]


# ---------------------------------------------------------------------------
# Message 5 — Storage
# ---------------------------------------------------------------------------


def _storage_topics(storage: dict) -> list[Topic]:
    """Build the full storage group: volumes, physical drives, BitLocker."""
    logical = storage.get("logical") or []
    physical = storage.get("physical") or []
    bitlocker = storage.get("bitlocker") or []

    volumes: list[str] = []
    if not logical:
        volumes.append(_field("Logical volumes", UNAVAILABLE))
    for drive in logical:
        if not drive.get("fixed"):
            continue
        volumes.append(f"- **{_code(_device(drive.get('device')))}** {_plain(drive.get('type'))} | "
                       f"{_plain(drive.get('fileSystem'))} | label {_plain(drive.get('label'))}")
        volumes.append(
            f"  total {_plain(drive.get('total'))} | used {_plain(drive.get('used'))} "
            f"({_plain(drive.get('usedPercent'))}) | free {_plain(drive.get('free'))}"
        )
    others = [drive for drive in logical if not drive.get("fixed")]
    if others:
        # Removable, network, and optical volumes carry little usable detail
        # on most machines, so they are grouped instead of repeated.
        volumes.append(
            "- **Other volumes:** "
            + ", ".join(
                f"{_code(_device(drive.get('device')))} ({_plain(drive.get('type'))}, "
                f"{_plain(drive.get('fileSystem'))})"
                for drive in others
            )
        )

    drives: list[str] = []
    if not physical:
        drives.append(_field("Physical drives", UNAVAILABLE))
    for disk in physical:
        drives.append(f"- **{_plain(disk.get('model'))}:** {_plain(disk.get('mediaType'))} | "
                      f"{_plain(disk.get('interface'))}")
        drives.append(
            f"  capacity {_plain(disk.get('size'))} | health {_health(disk.get('health'))} | "
            f"state {_plain(disk.get('operational'))} | temp {_plain(disk.get('temperature'))} | "
            f"wear {_plain(disk.get('wear'))}"
        )

    encryption: list[str] = []
    if not bitlocker:
        encryption.append(_field("BitLocker volumes", UNAVAILABLE))
    for volume in bitlocker:
        encryption.append(
            f"- **{_code(_device(volume.get('mountPoint')))}** protection {_plain(volume.get('protection'))} | "
            f"{_plain(volume.get('status'))} | {_plain(volume.get('encryption'))} | "
            f"{_plain(volume.get('percent'))}"
        )

    return [
        Topic(GROUP_STORAGE, "Logical Volumes", volumes),
        Topic(GROUP_STORAGE, "Physical Drives", drives),
        Topic(GROUP_STORAGE, "BitLocker", encryption),
    ]


# ---------------------------------------------------------------------------
# Message 6 — Network & Power
# ---------------------------------------------------------------------------


def _network_topics(network: dict, power: dict) -> list[Topic]:
    """Build the network adapters plus the power and battery group."""
    adapters = network.get("adapters") or []
    summary = [
        _field("Internet connectivity", network.get("internetConnectivity")),
        _quote_field("Public IPv4", network.get("publicIpv4")),
        _field("Network adapter count", str(len(adapters)) if adapters else UNAVAILABLE),
        _field("Discord gateway connection", _connection(network.get("discordConnection"))),
        _field("Discord gateway latency", network.get("discordLatency")),
    ]
    details: list[str] = []
    if not adapters:
        details.append(_field("Network adapters", UNAVAILABLE))
    for index, adapter in enumerate(adapters, start=1):
        if index > 1:
            details.append("")
        details.extend([
            f"**Adapter {index}: {_plain(adapter.get('name'))} - {_adapter_kind(adapter)}**",
            _field("Description", adapter.get("description")),
            _code_field("IPv4", adapter.get("ipv4")),
            _code_field("IPv6", adapter.get("ipv6")),
            _code_field("Subnet mask", adapter.get("subnet")),
            _code_field("Default gateway", adapter.get("gateway")),
            _code_field("DNS servers", adapter.get("dns")),
            f"**DHCP:** {_plain(adapter.get('dhcpEnabled'))} | "
            f"**DHCP server:** {_code(adapter.get('dhcpServer'))}",
            _code_field("MAC address", adapter.get("mac")),
            _field("Link speed", adapter.get("linkSpeed")),
            f"**Received:** {_plain(adapter.get('received'))} | "
            f"**Sent:** {_plain(adapter.get('sent'))}",
        ])

    return [
        Topic(GROUP_NETWORK, "Connectivity", summary),
        Topic(GROUP_NETWORK, "Network Adapters", details),
        Topic(GROUP_NETWORK, "Power & Battery", [
            _field("Power source", power.get("powerSource")),
            _field("Battery present", power.get("batteryPresent")),
            _field("Battery level", power.get("batteryLevel")),
            _field("Charging status", power.get("charging")),
            _field("Active power plan", power.get("powerPlan")),
            _field("Sleep / hibernate", power.get("sleepStates")),
        ]),
    ]


def _adapter_kind(adapter: dict) -> str:
    """Label an adapter with its link state and virtual/physical nature."""
    status = _plain(adapter.get("status"))
    virtual = adapter.get("virtual")
    if virtual == "Yes":
        kind = "virtual"
    elif virtual == "No":
        kind = "physical"
    else:
        kind = "type unknown"
    return f"{status}, {kind}"


# ---------------------------------------------------------------------------
# Message 7 — Security & Updates
# ---------------------------------------------------------------------------


def _security_topics(security: dict) -> list[Topic]:
    """Build the security and update group, including a platform summary."""
    profiles = security.get("firewallProfiles") or []
    firewall = (
        [_field(f"Firewall ({_plain(profile.get('profile'))})", profile.get("state"))
         for profile in profiles]
        or [_field("Windows Firewall", UNAVAILABLE)]
    )
    return [
        Topic(GROUP_SECURITY, "Windows Defender", [
            _field("Defender status", security.get("defenderStatus")),
            _field("Real-time protection", security.get("realTimeProtection")),
            _field("Signature version", security.get("defenderSignature")),
            _field("Signature updated", security.get("defenderSignatureDate")),
        ]),
        Topic(GROUP_SECURITY, "Windows Firewall", firewall),
        Topic(GROUP_SECURITY, "Windows Updates", [
            _field("Last installed update", security.get("lastUpdate")),
            _field("Installed hotfix count", security.get("hotfixCount")),
            _field("Pending updates", security.get("pendingUpdates")),
        ]),
        Topic(GROUP_SECURITY, "User Account Control", [
            _field("UAC", security.get("uacStatus")),
        ]),
        Topic(GROUP_SECURITY, "Platform Security", [
            # Secure Boot, TPM, and BitLocker detail lives in the firmware and
            # storage messages; this one line keeps the security overview
            # complete without repeating every field.
            f"**Secure Boot:** {_plain(security.get('secureBoot'))} | "
            f"**TPM:** {_plain(security.get('tpmStatus'))} | "
            f"**BitLocker:** {_plain(security.get('bitlockerStatus'))}",
        ]),
    ]


# ---------------------------------------------------------------------------
# Message 8 — Performance & Agent
# ---------------------------------------------------------------------------


def _agent_topics(performance: dict, agent: dict, environment: dict) -> list[Topic]:
    """Build the performance and agent diagnostics group."""
    return [
        Topic(GROUP_AGENT, "Performance", [
            _field("CPU utilization", performance.get("cpuUtilization")),
            _field("Memory utilization", performance.get("memoryUtilization")),
            _field("Disk read throughput", performance.get("diskRead")),
            _field("Disk write throughput", performance.get("diskWrite")),
            _field("Disk busy time", performance.get("diskBusy")),
            _field("Network received", performance.get("networkReceived")),
            _field("Network sent", performance.get("networkSent")),
            _field("Sampling window", performance.get("sampleWindow")),
            _field("Sample timestamp", performance.get("sampleTimestamp")),
        ]),
        Topic(GROUP_AGENT, "Astral Remote Agent", [
            _field("Agent version", agent.get("version")),
            _code_field("Installation ID", agent.get("installationId")),
            _field("Agent uptime", agent.get("uptime")),
            _field("Discord connection", _connection(agent.get("connection"))),
            _field("Discord latency", agent.get("latency")),
            _field("Reconnect count", agent.get("reconnectCount")),
            _field("Last successful update", agent.get("lastUpdate")),
            _field("Required management permission", agent.get("requiredPermission")),
            _field("Managed category status", agent.get("categoryStatus")),
            _field("Managed channel count", agent.get("channelCount")),
            _field("Duplicate category warning", agent.get("duplicateWarning")),
            _field("Startup registration", agent.get("startupRegistration")),
            _field("Backup executable", agent.get("backupExecutable")),
            _field("Diagnostic summary", agent.get("logSummary")),
        ]),
        Topic(GROUP_AGENT, "Agent Environment", [
            _field("Environment collection", environment.get("environmentStatus")),
            _code_field("Agent state directory", environment.get("stateDirectory")),
            _code_field("Diagnostic log", environment.get("diagnosticLog")),
        ]),
    ]


# ---------------------------------------------------------------------------
# Topics and message packing
# ---------------------------------------------------------------------------


def topics(inventory: dict) -> list[Topic]:
    """Return every report section in the fixed message-group order.

    The Overview is always first, so the published report always opens with
    the dashboard. Sections never carry duplicated facts: the operating
    system directories live in the OS group, and the Secure Boot, TPM, and
    BitLocker details live in the graphics/firmware and storage groups,
    leaving the security group a single combined platform line.
    """
    inventory = inventory or {}
    return [
        *_overview_topics(inventory),
        *_windows_topics(inventory.get("os") or {}),
        *_compute_topics(inventory.get("cpu") or {}, inventory.get("memory") or {}),
        *_graphics_topics(inventory.get("graphics") or [], inventory.get("board") or {}),
        *_storage_topics(inventory.get("storage") or {}),
        *_network_topics(inventory.get("network") or {}, inventory.get("power") or {}),
        *_security_topics(inventory.get("security") or {}),
        *_agent_topics(
            inventory.get("performance") or {},
            inventory.get("agent") or {},
            inventory.get("environment") or {},
        ),
    ]


def build_blocks(inventory: dict) -> list[Block]:
    """Turn one collected inventory into ordered, size-limited blocks."""
    blocks: list[Block] = []
    for topic in topics(inventory):
        blocks.extend(_topic_blocks(topic))
    return blocks


def _flatten(lines: list[str]) -> list[str]:
    """Hard-wrap any single line that alone would exceed one block."""
    result: list[str] = []
    for line in lines:
        while len(line) > SECTION_CHUNK:
            result.append(line[:SECTION_CHUNK])
            line = line[SECTION_CHUNK:]
        result.append(line)
    return result


def _topic_block(topic: Topic, lines: list[str], first: bool) -> Block:
    """Build one section body.

    The first chunk opens the section with its heading; later chunks are plain
    bodies with no heading at all, so a section title is printed exactly once
    and never repeated inside or across messages.
    """
    body = "\n".join(lines)
    if first and topic.heading:
        return Block(
            text=f"### {topic.title}\n{body}",
            group=topic.group,
            heading=True,
            section=topic.title,
            level=topic.level,
        )
    if first:
        return Block(
            text=body,
            group=topic.group,
            heading=False,
            section=topic.title,
            level=topic.level,
        )
    return Block(
        text=body,
        group=topic.group,
        continuation=True,
        section=topic.title,
        level=topic.level,
    )


def _topic_blocks(topic: Topic) -> list[Block]:
    """Split one section into body blocks that stay under one message budget."""
    if not topic.lines:
        return []
    blocks: list[Block] = []
    chunk: list[str] = []
    length = 0
    first = True
    for line in _flatten(topic.lines):
        cost = len(line) + 1
        if chunk and length + cost > SECTION_CHUNK:
            blocks.append(_topic_block(topic, chunk, first))
            first = False
            chunk = []
            length = 0
        chunk.append(line)
        length += cost
    if chunk:
        blocks.append(_topic_block(topic, chunk, first))
    return blocks


def pack_blocks(blocks: list[Block]) -> list[list[Block]]:
    """Pack blocks into pages that respect every Discord message limit.

    A new page always starts at a message-group boundary, so each group is
    published as its own message (or as its own run of messages when the
    group is long). Blocks are never reordered, split, or dropped, so the
    published report always contains every item exactly once and in order.
    Every page reserves room for the message title, the group heading, and a
    section title repeated when a body continues at the top of that page;
    all three are added at view-build time, after packing.
    """
    pages: list[list[Block]] = []
    page: list[Block] = []
    length = PAGE_RESERVE_TEXT
    children = PAGE_RESERVE_COMPONENTS
    for block in blocks:
        cost = len(block.text)
        added = 2  # one TextDisplay plus its separator
        starts_group = bool(page) and page[-1].group != block.group
        over_limit = bool(page) and (length + cost > TEXT_BUDGET
                                     or children + added > COMPONENT_BUDGET)
        if page and (starts_group or over_limit):
            pages.append(page)
            page = []
            length = PAGE_RESERVE_TEXT
            children = PAGE_RESERVE_COMPONENTS
        page.append(block)
        length += cost
        children += added
    if page:
        pages.append(page)
    return pages


def build_report(inventory: dict) -> tuple[list[Block], list[list[Block]]]:
    """Build the ordered blocks and the packed message pages for one inventory."""
    blocks = build_blocks(inventory)
    return blocks, pack_blocks(blocks)


# ---------------------------------------------------------------------------
# Components V2 views
# ---------------------------------------------------------------------------


def _part_title(index: int, total: int) -> str:
    """Render the message title, which carries its report position."""
    return f"# {TITLE} (Part {index + 1} of {total})"


def _page_level(page: list[Block]):
    """Return the highest severity carried by any block on the page."""
    level = None
    for block in page:
        if _LEVEL_RANK.get(block.level, 0) > _LEVEL_RANK.get(level, 0):
            level = block.level
    return level


def build_views(pages: list[list[Block]]) -> list:
    """Build one Components V2 LayoutView per packed page, in order.

    Every message opens with its title and its group heading, so the reader
    can always identify the message and its topic. Continuation is carried by
    the title alone: a group or section that continues from an earlier message
    is never re-labelled, so no heading is ever repeated with a "(continued)"
    marker. The container accent colour reflects the highest severity on the
    page, always alongside the matching text. No select menu, button, or other
    interactive control is ever added.
    """
    import discord
    from discord import ui

    total = len(pages)
    views = []
    for index, page in enumerate(pages):
        group = page[0].group
        children = [ui.TextDisplay(_part_title(index, total))]
        if group:
            children.append(ui.TextDisplay(f"## {group}"))
        for position, block in enumerate(page):
            if position > 0 or group:
                children.append(ui.Separator(spacing=discord.SeparatorSpacing.small))
            children.append(ui.TextDisplay(block.text))
        colour = LEVEL_COLOURS.get(_page_level(page), ACCENT_COLOUR)
        view = ui.LayoutView(timeout=None)
        view.add_item(ui.Container(*children, accent_colour=discord.Colour(colour)))
        views.append(view)
    return views


def measure_view(view) -> dict:
    """Measure one LayoutView against the Discord message limits."""
    text_length = view.content_length()
    components = view.total_children_count
    return {
        "textLength": text_length,
        "components": components,
        "textOk": text_length <= MAX_TEXT_LENGTH,
        "componentsOk": components <= MAX_COMPONENTS,
    }


def validate_views(views: list) -> list[dict]:
    """Measure every view that is about to be sent, warning on overruns.

    The report builder already sizes pages to the budgets, so a violation
    here means a bug: it is reported instead of being sent silently.
    """
    reports = []
    for index, view in enumerate(views):
        report = measure_view(view)
        report["index"] = index
        if not (report["textOk"] and report["componentsOk"]):
            warn(
                "Info report part exceeds Discord limits.",
                {"part": index, "text": report["textLength"], "components": report["components"]},
            )
        reports.append(report)
    return reports


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


def _owned_by_client(message, channel) -> bool:
    """True only when this bot account authored the message."""
    author = getattr(message, "author", None)
    me = getattr(getattr(channel, "guild", None), "me", None)
    if author is not None and me is not None:
        return getattr(author, "id", None) == getattr(me, "id", None)
    return bool(getattr(author, "bot", False))


async def _fetch_owned(channel, message_id):
    """Fetch a tracked message, or None when it is gone or not ours."""
    fetch = getattr(channel, "fetch_message", None)
    if not callable(fetch):
        return None
    try:
        message = await fetch(message_id)
    except Exception:  # noqa: BLE001 - a deleted message simply cannot be reused
        return None
    return message if _owned_by_client(message, channel) else None


async def _edit_view(message, view) -> bool:
    """Replace a tracked message's content with the new view."""
    edit = getattr(message, "edit", None)
    if not callable(edit):
        return False
    try:
        # The old report used message content; clearing it is required
        # before the same message can carry Components V2.
        await edit(content=None, view=view)
        return True
    except Exception as err:  # noqa: BLE001 - fall back to sending a fresh message
        warn("Info report message could not be updated.", {"message": str(err)})
        return False


async def _delete_tracked(channel, message_ids) -> list[int]:
    """Delete tracked bot messages, best-effort. Returns the deleted IDs."""
    removed = []
    for message_id in message_ids:
        message = await _fetch_owned(channel, message_id)
        if message is None:
            continue
        delete = getattr(message, "delete", None)
        if not callable(delete):
            continue
        try:
            await delete()
            removed.append(message_id)
        except Exception as err:  # noqa: BLE001 - cleanup never aborts publication
            warn("Stale info report message could not be removed.", {"message": str(err)})
    return removed


async def _send_views(channel, views: list, start: int) -> tuple[list[int], int | None]:
    """Send views from `start` in order. Returns (message IDs, failed index)."""
    sent: list[int] = []
    for index in range(start, len(views)):
        try:
            message = await channel.send(view=views[index])
        except Exception as err:  # noqa: BLE001 - a failed part is reported, not hidden
            warn(
                "Info report part failed to send.",
                {"part": index, "total": len(views), "message": str(err)},
            )
            return sent, index
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            sent.append(message_id)
    return sent, None


def _tracked_ids(store, guild_id) -> list[int]:
    """Read the tracked report message IDs, tolerating a missing store."""
    reader = getattr(store, "info_messages", None)
    if not callable(reader):
        return []
    try:
        tracked = reader(guild_id)
    except Exception:  # noqa: BLE001 - unreadable state means publish fresh
        return []
    return [item for item in (tracked or []) if isinstance(item, int) and not isinstance(item, bool)]


def _remember(store, guild_id, message_ids) -> None:
    """Persist the published message IDs so a restart can refresh them."""
    remember = getattr(store, "remember_info_messages", None)
    if not callable(remember):
        return
    try:
        remember(guild_id, message_ids)
    except Exception as err:  # noqa: BLE001 - losing the IDs only costs a repost
        warn("Info report tracking could not be saved.", {"message": str(err)})


async def publish_report(channel, views: list, store, guild_id) -> dict:
    """Publish the report views into one channel, in order.

    Messages tracked from a previous run are reused in place, so a
    restart refreshes the report instead of duplicating it. When a
    tracked message is gone, or the report needs more messages than
    before, the remaining tracked messages are removed first so the fresh
    sends stay in sequence. Only messages authored by this bot are ever
    edited or deleted, and a failed part stops the run so a later part
    can never appear before a missing one.
    """
    validate_views(views)
    tracked = _tracked_ids(store, guild_id)
    published: list[int] = []
    deleted: list[int] = []
    reused = 0
    for index, view in enumerate(views):
        if index < len(tracked):
            message = await _fetch_owned(channel, tracked[index])
            if message is not None and await _edit_view(message, view):
                published.append(tracked[index])
                reused += 1
                continue
        deleted.extend(await _delete_tracked(channel, tracked[index:]))
        sent, failed_at = await _send_views(channel, views, index)
        published.extend(sent)
        _remember(store, guild_id, published)
        if failed_at is not None:
            warn(
                "Info report published partially.",
                {"guild": guild_id, "failedPart": failed_at, "total": len(views)},
            )
        return {"published": published, "failedAt": failed_at, "deleted": deleted, "reused": reused}
    deleted.extend(await _delete_tracked(channel, tracked[len(views):]))
    _remember(store, guild_id, published)
    return {"published": published, "failedAt": None, "deleted": deleted, "reused": reused}
