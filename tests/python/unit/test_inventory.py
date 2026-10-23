# Unit tests for the read-only Windows system inventory collector.
# The PowerShell CIM reply is injected and every probe that samples, scans,
# or reaches the network is stubbed, so these tests are deterministic and
# completely offline.

import base64
import datetime
import json

import pytest

from astral_remote.agent.windows import inventory as inv

AGENT = {
    "hostname": "DESKTOP-TEST",
    "installationId": "0123456789abcdef",
    "localIpv4": "192.168.1.20",
    "connected": True,
    "latencySeconds": 0.042,
    "reconnects": 0,
    "uptimeSeconds": 3725,
    "categoryStatus": "Created new category (id 1)",
    "channelCount": 1,
    "registration": {
        "valueName": "astral-remote",
        "runKey": "registered",
        "logonTask": "present",
        "backupPresent": True,
    },
    "logSummary": {"present": False, "entries": 0, "warnings": 0, "fatals": 0, "lastTimestamp": None},
}

PS = {
    "os": {
        "Caption": "Microsoft Windows 11 Pro",
        "Version": "10.0.22631",
        "BuildNumber": "22631",
        "OSArchitecture": "64-bit",
        "BuildType": "Multiprocessor Free",
        "ProductType": 1,
        "WindowsDirectory": "C:\\Windows",
        "SystemDirectory": "C:\\Windows\\system32",
        "BootDevice": "\\Device\\HarddiskVolume3",
        "Manufacturer": "Microsoft Corporation",
        "InstallDate": "2024-01-02 03:04:05",
    },
    "computer": {
        "Manufacturer": "Contoso",
        "Model": "Workstation 9000",
        "SystemType": "x64-based PC",
        "Domain": "EXAMPLE",
        "PartOfDomain": False,
    },
    "bios": {"Manufacturer": "Contoso", "SMBIOSBIOSVersion": "2.4.1", "ReleaseDate": "2024-05-06"},
    "board": {"Manufacturer": "Contoso", "Product": "X570-A", "Version": "1.0"},
    "cpu": [
        {
            "Name": "AMD Ryzen 7 5800X 8-Core Processor",
            "Manufacturer": "AuthenticAMD",
            "NumberOfCores": 8,
            "NumberOfLogicalProcessors": 16,
            "MaxClockSpeed": 3800,
            "CurrentClockSpeed": 3700,
            "L2CacheSize": 4096,
            "L3CacheSize": 32768,
            "Architecture": 9,
            "VirtualizationFirmwareEnabled": True,
            "SecondLevelAddressTranslationExtensions": True,
            "VMMonitorModeExtensions": False,
        }
    ],
    "memoryModules": [
        {
            "Manufacturer": "Kingston",
            "PartNumber": "KF3200C16",
            "Capacity": 8589934592,
            "Speed": 3200,
            "SMBIOSMemoryType": 26,
            "DeviceLocator": "DIMM 0",
        }
    ],
    "pageFiles": [{"Name": "C:\\pagefile.sys", "AllocatedBaseSize": 4096, "CurrentUsage": 1024}],
    "gpus": [
        {
            "Name": "NVIDIA GeForce RTX 3070",
            "AdapterCompatibility": "NVIDIA",
            "DriverVersion": "31.0.15.5161",
            "DriverDate": "2023-11-01",
            "AdapterRAM": 8589934592,
            "CurrentHorizontalResolution": 2560,
            "CurrentVerticalResolution": 1440,
            "CurrentRefreshRate": 144,
            "Status": "OK",
        }
    ],
    "logicalDisks": [
        {
            "DeviceID": "C:",
            "DriveType": 3,
            "FileSystem": "NTFS",
            "FileSystemLabel": "System",
            "Size": 500107862016,
            "FreeSpace": 250053931008,
        }
    ],
    "physicalDisks": [
        {
            "FriendlyName": "Contoso NVMe SSD",
            "MediaType": "SSD",
            "BusType": "NVMe",
            "Size": 500107862016,
            "HealthStatus": "Healthy",
            "OperationalStatus": "OK",
        }
    ],
    "diskHealth": [{"FriendlyName": "Contoso NVMe SSD", "Temperature": 41, "Wear": 3}],
    "bitlocker": [
        {
            "MountPoint": "C:",
            "VolumeStatus": 1,
            "ProtectionStatus": 1,
            "EncryptionPercentage": 100,
            "EncryptionMethod": 6,
        },
        {
            "MountPoint": "D:",
            "VolumeStatus": 0,
            "ProtectionStatus": 0,
            "EncryptionPercentage": 0,
            "EncryptionMethod": 0,
        },
    ],
    "adapters": [
        {
            "Name": "Ethernet",
            "InterfaceDescription": "Contoso GbE",
            "Status": "Disconnected",
            "MacAddress": "AA-BB-CC-DD-EE-FF",
            "Virtual": False,
            "ifIndex": 9,
        },
        {
            "Name": "Ethernet 2",
            "InterfaceDescription": "Contoso GbE 2",
            "Status": "Up",
            "LinkSpeed": "1 Gbps",
            "MacAddress": "11-22-33-44-55-66",
            "Virtual": False,
            "ifIndex": 4,
        },
    ],
    "ipAddresses": [
        {"InterfaceIndex": 4, "IPAddress": "192.168.1.20", "PrefixLength": 24, "AddressFamily": 2},
        {"InterfaceIndex": 4, "IPAddress": "fe80::1", "PrefixLength": 64, "AddressFamily": 23},
    ],
    "routes": [{"InterfaceIndex": 4, "NextHop": "192.168.1.1"}],
    "dns": [{"InterfaceIndex": 4, "ServerAddresses": ["192.168.1.1", "1.1.1.1"]}],
    "ipInterfaces": [{"InterfaceIndex": 4, "Dhcp": 1}],
    "adapterStats": [{"Name": "Ethernet 2", "ReceivedBytes": 1073741824, "SentBytes": 536870912}],
    "adapterConfig": [
        {
            "Index": 4,
            "Description": "Contoso GbE 2",
            "DHCPServer": "192.168.1.1",
            "DHCPEnabled": True,
            "IPSubnet": ["255.255.255.0"],
            "DefaultIPGateway": ["192.168.1.1"],
        }
    ],
    "powerPlan": {"ElementName": "Balanced"},
    "tpm": {"IsEnabled_InitialValue": True, "SpecVersion": "2.0", "ManufacturerVersion": "7.2"},
    "secureBoot": True,
    "defender": {
        "AntivirusEnabled": True,
        "RealTimeProtectionEnabled": True,
        "AntivirusSignatureVersion": "1.400.1",
        "AntivirusSignatureLastUpdated": "2026-01-02 03:04:05",
    },
    "firewall": [{"Name": "Domain", "Enabled": True}, {"Name": "Public", "Enabled": False}],
    "hotfixes": [
        {"HotFixID": "KB5034441", "InstalledOn": "2024-01-10"},
        {"HotFixID": "KB5034765", "InstalledOn": "2024-02-14"},
    ],
    "diskPerf": [
        {"Name": "_Total", "DiskReadBytesPerSec": 1048576, "DiskWriteBytesPerSec": 524288, "PercentDiskTime": 7}
    ],
    "netPerf": [{"Name": "Ethernet 2", "BytesReceivedPersec": 2048, "BytesSentPersec": 1024}],
    "thermal": [{"InstanceName": "ACPI\\ThermalZone\\TZ00", "CurrentTemperature": 3132}],
    "culture": {"Name": "en-US", "DisplayName": "English (United States)"},
    "timeZone": {"Id": "Pacific Standard Time", "DisplayName": "(UTC-08:00) Pacific Time"},
}


@pytest.fixture
def stub_probes(monkeypatch):
    """Replace every probe that samples hardware, scans disk, or uses the network."""
    monkeypatch.setattr(inv, "read_public_ipv4", lambda timeout=0: "203.0.113.9")
    monkeypatch.setattr(inv, "_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr(inv, "read_cpu_utilization", lambda window=0: 12.5)
    monkeypatch.setattr(inv, "read_memory_status", lambda: {
        "loadPercent": 45.0,
        "totalPhysical": 17179869184,
        "availablePhysical": 8589934592,
        "commitLimit": 25769803776,
        "commitAvailable": 17179869184,
    })
    monkeypatch.setattr(inv, "read_power_status", lambda: {
        "acLine": 1,
        "batteryFlag": 128,
        "batteryPercent": 255,
        "batterySeconds": 0,
    })
    return inv


def collect(ps=None, agent=None, **kwargs):
    """Collect one inventory from an injected CIM reply."""
    moment = kwargs.pop("now", datetime.datetime(2026, 3, 4, 5, 6, 7))
    return inv.collect(agent or AGENT, ps_data=ps if ps is not None else PS, env={}, now=moment, **kwargs)


def test_collect_returns_every_section(stub_probes):
    data = collect()
    assert set(data) == {
        "identity", "os", "cpu", "memory", "graphics", "board", "storage", "network",
        "power", "security", "performance", "environment", "agent",
    }
    # Process, service, and installed-software inventories are not collected.
    assert "processes" not in data
    assert "services" not in data
    assert "software" not in data


def test_identity_prefers_injected_agent_facts(stub_probes):
    identity = collect()["identity"]
    assert identity["hostname"] == "DESKTOP-TEST"
    assert identity["installationId"] == "0123456789abcdef"
    assert identity["agentStatus"] == "Connected"
    assert identity["agentVersion"] == "0.1.0"
    assert identity["osName"] == "Microsoft Windows 11 Pro"
    assert identity["osVersion"].startswith("10.0.22631")
    assert identity["lastUpdated"] == "2026-03-04 05:06:07"
    # The header carries only identity plus headline numbers, so the
    # hardware facts that used to live here are not duplicated.
    assert "manufacturer" not in identity
    assert "pid" not in identity


def test_offline_agent_reports_disconnected_status(stub_probes):
    identity = collect(agent=dict(AGENT, connected=False))["identity"]
    assert identity["agentStatus"] == "Disconnected"


def test_unknown_agent_state_is_not_reported_as_connected(stub_probes):
    agent = {key: value for key, value in AGENT.items() if key != "connected"}
    data = collect(agent=agent)
    assert data["identity"]["agentStatus"] == "Unknown"
    assert data["network"]["discordConnection"] == "Unknown"
    assert data["agent"]["connection"] == "Unknown"


def test_os_section_renders_cim_values(stub_probes):
    os_info = collect()["os"]
    assert os_info["osName"] == "Microsoft Windows 11 Pro"
    assert os_info["osManufacturer"] == "Microsoft Corporation"
    assert os_info["osVersion"] == "10.0.22631"
    assert os_info["osArchitecture"] == "64-bit"
    assert os_info["osConfiguration"] == "Workstation"
    assert os_info["osBuildType"] == "Multiprocessor Free"
    assert os_info["systemLocale"] == "English (United States)"
    assert os_info["inputLocale"] == "en-US"
    assert os_info["timeZone"] == "Pacific Standard Time ((UTC-08:00) Pacific Time)"
    assert os_info["domainWorkgroup"] == "EXAMPLE"
    assert os_info["partOfDomain"] == "No"
    assert os_info["bootDevice"] == "\\Device\\HarddiskVolume3"
    assert os_info["windowsDirectory"] == "C:\\Windows"
    assert os_info["firmwareArchitecture"] == "x64-based PC"


def test_cpu_section_sums_cores_and_converts_thermal(stub_probes):
    cpu = collect()["cpu"]
    assert cpu["manufacturer"] == "AuthenticAMD"
    assert cpu["model"] == "AMD Ryzen 7 5800X 8-Core Processor"
    assert cpu["architecture"] == "x64"
    assert cpu["physicalCores"] == "8"
    assert cpu["logicalProcessors"] == "16"
    assert cpu["maxClock"] == "3800 MHz (3.80 GHz)"
    assert cpu["currentClock"] == "3700 MHz (3.70 GHz)"
    assert cpu["l2Cache"] == "4096 KB"
    assert cpu["l3Cache"] == "32768 KB"
    assert cpu["virtualizationFirmware"] == "Yes"
    assert cpu["virtualizationExtensions"] == "Yes"
    assert cpu["utilization"] == "12.5%"
    # 3132 tenths of Kelvin is about 40 C.
    assert cpu["temperature"].startswith("40") and cpu["temperature"].endswith(" C")


def test_memory_section_includes_modules_and_page_file(stub_probes):
    memory = collect()["memory"]
    assert memory["totalPhysical"] == "16.0 GB"
    assert memory["usedPhysical"] == "8.0 GB"
    assert memory["availablePhysical"] == "8.0 GB"
    assert memory["usagePercent"] == "45.0%"
    assert memory["totalVirtual"] == "24.0 GB"
    assert memory["pageFileSize"] == "4.0 GB"
    assert memory["pageFileUsage"] == "1.0 GB"
    assert memory["moduleCount"] == "1"
    module = memory["modules"][0]
    assert module["locator"] == "DIMM 0"
    assert module["capacity"] == "8.0 GB"
    assert module["type"] == "DDR4"
    assert module["speed"] == "3200 MT/s"
    assert module["partNumber"] == "KF3200C16"


def test_graphics_section_reports_unsupported_meters(stub_probes):
    gpu = collect()["graphics"][0]
    assert gpu["name"] == "NVIDIA GeForce RTX 3070"
    assert gpu["resolution"] == "2560 x 1440"
    assert gpu["refreshRate"] == "144 Hz"
    # Windows exposes no per-GPU utilization or temperature here.
    assert gpu["utilization"] == inv.NOT_SUPPORTED
    assert gpu["temperature"] == inv.NOT_SUPPORTED
    assert gpu["sharedMemory"] == inv.NOT_SUPPORTED


def test_storage_section_translates_enums(stub_probes):
    storage = collect()["storage"]
    volume = storage["logical"][0]
    assert volume["device"] == "C:"
    assert volume["type"] == "Local disk"
    assert volume["label"] == "System"
    assert volume["usedPercent"].startswith("50")
    assert volume["fixed"] is True
    assert 49.0 < volume["percentValue"] < 51.0
    disk = storage["physical"][0]
    assert disk["model"] == "Contoso NVMe SSD"
    assert disk["mediaType"] == "SSD"
    assert disk["busType"] == "NVMe"
    assert disk["health"] == "Healthy"
    assert disk["temperature"] == "41 C"
    assert disk["wear"] == "3%"
    encrypted, plain = storage["bitlocker"]
    assert (encrypted["status"], encrypted["protection"], encrypted["encryption"]) == (
        "Fully encrypted", "On", "XTS-AES-128",
    )
    assert encrypted["percent"] == "100%" and encrypted["protected"] is True
    assert (plain["status"], plain["protection"], plain["encryption"]) == ("Fully decrypted", "Off", "None")
    assert plain["protected"] is False
    # The removed headline summary is not rebuilt anywhere; the Overview reads
    # the volume numbers straight from the section instead.
    assert "summary" not in storage


def test_removable_volumes_are_not_flagged_fixed(stub_probes):
    storage = collect(ps=dict(PS, logicalDisks=[
        {"DeviceID": "C:", "DriveType": 3, "FileSystem": "NTFS", "FileSystemLabel": "System",
         "Size": 500107862016, "FreeSpace": 250053931008},
        {"DeviceID": "D:", "DriveType": 2, "FileSystem": "FAT32", "FileSystemLabel": "",
         "Size": 32000000000, "FreeSpace": 16000000000},
    ]))["storage"]
    assert [drive["fixed"] for drive in storage["logical"]] == [True, False]


def test_network_section_lists_adapters_up_first(stub_probes):
    network = collect()["network"]
    assert [adapter["name"] for adapter in network["adapters"]] == ["Ethernet 2", "Ethernet"]
    online = network["adapters"][0]
    assert online["status"] == "Up"
    assert online["ipv4"] == "192.168.1.20/24"
    assert online["ipv6"] == "fe80::1"
    assert online["subnet"] == "255.255.255.0"
    assert online["gateway"] == "192.168.1.1"
    assert online["dns"] == "192.168.1.1, 1.1.1.1"
    assert online["dhcpEnabled"] == "Yes"
    assert online["dhcpServer"] == "192.168.1.1"
    assert online["received"] == "1.0 GB"
    assert online["sent"] == "512.0 MB"
    offline = network["adapters"][1]
    assert offline["ipv4"] == inv.UNAVAILABLE and offline["ipv6"] == "None"
    assert network["internetConnectivity"] == "Reachable"
    assert network["publicIpv4"] == "203.0.113.9"
    assert network["discordConnection"] == "Connected"
    assert network["discordLatency"] == "42 ms"
    assert collect(agent=dict(AGENT, connected=False))["network"]["discordConnection"] == "Disconnected"


def test_network_section_reports_lookup_failure_without_faking(stub_probes, monkeypatch):
    monkeypatch.setattr(inv, "read_public_ipv4", lambda timeout=0: None)
    network = collect()["network"]
    assert network["publicIpv4"] == inv.UNAVAILABLE
    assert network["internetConnectivity"] == "Reachable (public address lookup failed)"


def test_power_section_uses_battery_when_present(stub_probes):
    power = collect(ps=dict(PS, battery={"Name": "Contoso Battery", "BatteryStatus": 2,
                                       "EstimatedChargeRemaining": 88}))["power"]
    assert power["powerSource"] == "AC power"
    assert power["batteryPresent"] == "Yes"
    assert power["batteryLevel"] == "88%"
    assert power["charging"] == "On AC power"
    assert power["powerPlan"] == "Balanced"
    assert power["sleepStates"] == inv.NOT_SUPPORTED


def test_power_section_marks_desktop_battery_not_applicable(stub_probes):
    power = collect()["power"]
    assert power["batteryPresent"] == inv.NOT_APPLICABLE
    assert power["batteryLevel"] == inv.NOT_APPLICABLE
    assert power["charging"] == inv.NOT_APPLICABLE


def test_security_section_summarizes_defender_and_updates(stub_probes):
    security = collect()["security"]
    assert security["defenderStatus"] == "Enabled (real-time on)"
    assert security["realTimeProtection"] == "Yes"
    assert security["defenderSignature"] == "1.400.1"
    assert security["defenderSignatureDate"] == "2026-01-02 03:04:05"
    assert security["firewallProfiles"] == [
        {"profile": "Domain", "state": "On"},
        {"profile": "Public", "state": "Off"},
    ]
    assert security["lastUpdate"] == "KB5034765 (2024-02-14)"
    assert security["hotfixCount"] == "2"
    assert security["pendingUpdates"] == inv.NOT_SUPPORTED
    assert security["secureBoot"] == "Yes"
    assert security["tpmStatus"] == "Yes (spec 2.0)"
    assert security["bitlockerStatus"] == "1/2 volumes protected"


def test_performance_section_renders_counters(stub_probes):
    performance = collect()["performance"]
    assert performance["cpuUtilization"] == "12.5%"
    assert performance["memoryUtilization"] == "45.0%"
    assert performance["diskRead"] == "1.0 MB/s"
    assert performance["diskWrite"] == "512.0 KB/s"
    assert performance["diskBusy"] == "7.0%"
    assert performance["networkReceived"] == "2.0 KB/s"
    assert performance["networkSent"] == "1.0 KB/s"
    assert performance["sampleWindow"] == "250 ms"
    assert performance["sampleTimestamp"] == "2026-03-04 05:06:07"


def test_board_section_keeps_system_and_motherboard_facts(stub_probes):
    board = collect()["board"]
    assert board["systemManufacturer"] == "Contoso"
    assert board["systemModel"] == "Workstation 9000"
    assert board["systemType"] == "x64-based PC"
    assert board["boardManufacturer"] == "Contoso"
    assert board["boardModel"] == "X570-A"


def test_agent_section_reports_version_and_registration(stub_probes):
    agent = collect()["agent"]
    assert agent["version"] == "0.1.0"
    assert agent["installationId"] == "0123456789abcdef"
    assert agent["uptime"] == "1h 2m 5s"
    # The Agent process ID is no longer collected or reported.
    assert "pid" not in agent
    assert agent["connection"] == "Connected"
    assert agent["latency"] == "42 ms"
    assert agent["reconnectCount"] == "0"
    assert agent["lastUpdate"] == inv.NOT_SUPPORTED
    # The only operator command is the read-only `status` in #main, so the
    # report states that no management permission is required.
    assert "commands" not in agent
    assert agent["requiredPermission"] == "N/A (status needs no management permission)"
    assert agent["categoryStatus"] == "Created new category (id 1)"
    assert agent["channelCount"] == "1"
    assert agent["duplicateWarning"] == "None detected"
    assert agent["startupRegistration"] == "Run key: registered | Logon task: present"
    assert agent["backupExecutable"] == "Present"
    assert agent["logSummary"] == "No diagnostic entries"


def test_environment_section_withholds_values(stub_probes, tmp_path):
    (tmp_path / "AstralRemote").mkdir()
    data = inv.collect(
        dict(AGENT, logSummary={"present": True, "entries": 3, "warnings": 2, "fatals": 1,
                                "lastTimestamp": "2026-01-01T00:00:00+00:00"}),
        ps_data=PS,
        env={"LOCALAPPDATA": str(tmp_path)},
        now=datetime.datetime(2026, 3, 4, 5, 6, 7),
    )
    environment = data["environment"]
    assert environment["environmentStatus"] == "Collected (values withheld)"
    assert environment["stateDirectory"] == "Available"
    assert environment["diagnosticLog"] == "Not created yet"
    # The Windows and system directories are reported once, by the OS
    # section, and are not duplicated here.
    assert "windowsDirectory" not in environment
    assert "systemDirectory" not in environment
    assert "bootDevice" not in environment
    assert "firmwareArchitecture" not in environment
    assert "softwareStatus" not in environment
    assert data["agent"]["logSummary"] == (
        "3 entries (2 warnings, 1 fatal); last 2026-01-01T00:00:00+00:00"
    )


def test_missing_cim_data_degrades_to_unavailable(stub_probes):
    data = collect(ps={})
    assert data["storage"]["logical"] == []
    assert data["storage"]["physical"] == []
    assert data["storage"]["bitlocker"] == []
    assert data["graphics"] == []
    assert data["network"]["adapters"] == []
    assert data["security"]["firewallProfiles"] == []
    assert data["security"]["hotfixCount"] == inv.UNAVAILABLE
    assert data["security"]["defenderStatus"] == inv.UNAVAILABLE
    assert data["board"]["tpmPresent"] == inv.UNAVAILABLE
    # A missing Confirm-SecureBootUEFI result is not reported as "No".
    assert data["board"]["secureBoot"] == inv.UNAVAILABLE
    assert data["power"]["powerPlan"] == inv.UNAVAILABLE
    assert data["identity"]["agentStatus"] == "Connected"


def test_inventory_never_carries_secret_shaped_fields(stub_probes):
    text = json.dumps(collect(), ensure_ascii=False).lower()
    for forbidden in ("password", "token", "productkey", "licensekey", "secret",
                      "credential", "apikey", "commandline", "clipboard"):
        assert forbidden not in text


def test_formatters_stay_honest_about_unknown_values():
    assert inv.format_bytes(0) == "0 B"
    assert inv.format_bytes(1024) == "1.0 KB"
    assert inv.format_bytes(-5) == inv.UNAVAILABLE
    assert inv.format_bytes(None) == inv.UNAVAILABLE
    assert inv.format_percent(0) == "0.0%"
    assert inv.format_percent("nope") == inv.UNAVAILABLE
    assert inv.format_duration(90061) == "1d 1h 1m 1s"
    assert inv.format_duration(-1) == inv.UNAVAILABLE
    assert inv.format_flag(True) == "Yes"
    assert inv.format_flag(False) == "No"
    assert inv.format_flag(None) == inv.UNAVAILABLE
    assert inv.format_text("   ") == inv.UNAVAILABLE
    assert inv.format_moment("2024-05-06") == "2024-05-06"
    assert inv.format_moment("05/06/2024") == "2024-05-06"
    assert inv.format_moment("2024-05-06 07:08:09") == "2024-05-06 07:08:09"
    assert inv.format_moment("") == inv.UNAVAILABLE


def test_first_available_value_skips_the_unavailable_marker():
    assert inv._pick(inv.UNAVAILABLE, "real") == "real"
    assert inv._pick(None, "") == inv.UNAVAILABLE
    assert inv._pick("first", "second") == "first"
    assert inv._pick(7, inv.UNAVAILABLE) == "7"


def test_removed_process_service_and_software_helpers_are_gone():
    assert not hasattr(inv, "_command_executable")
    assert not hasattr(inv, "_default_browser")
    assert not hasattr(inv, "collect_software")
    assert not hasattr(inv, "collect_start_menu_count")
    assert not hasattr(inv, "build_process_section")
    assert not hasattr(inv, "build_service_section")
    assert not hasattr(inv, "build_software_section")


def test_powershell_child_runs_without_a_console_window(monkeypatch):
    """A --noconsole agent must never flash a console for its children."""
    from astral_remote.shared import winproc

    captured = {}

    class _Proc:
        stdout = base64.b64encode(b"{}")

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(inv.subprocess, "run", fake_run)
    assert inv.run_powershell_json("Write-Output 'x'") == {}
    assert winproc.CREATE_NO_WINDOW
    assert captured["creationflags"] & winproc.CREATE_NO_WINDOW
    assert captured["shell"] is False
    assert captured["capture_output"] is True


def test_directory_availability_never_creates_anything(tmp_path):
    assert inv.directory_availability(None) == inv.UNAVAILABLE
    assert inv.directory_availability(tmp_path / "absent") == "Not created yet"
    assert not (tmp_path / "absent").exists()
    assert inv.directory_availability(tmp_path) == "Available"


def test_run_powershell_json_decodes_base64_and_survives_failure():
    script = "Write-Output 'x'"
    expected = {"os": {"Caption": "Test"}}
    payload = base64.b64encode(json.dumps(expected).encode("utf-8"))
    commands = []

    def runner(command):
        commands.append(command)
        return payload

    assert inv.run_powershell_json(script, runner=runner) == expected
    command = commands[0]
    assert command[0] == "powershell.exe"
    assert "-EncodedCommand" in command
    assert "shell" not in command
    assert base64.b64decode(command[-1]).decode("utf-16-le") == script

    def boom(command):
        raise TimeoutError("timed out")

    assert inv.run_powershell_json(script, runner=lambda command: b"") == {}
    assert inv.run_powershell_json(script, runner=lambda command: base64.b64encode(b"not json")) == {}
    assert inv.run_powershell_json(script, runner=boom) == {}
    assert inv.run_powershell_json(script, runner=lambda command: base64.b64encode(b"[1, 2]")) == {}
