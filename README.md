# ASTRAL REMOTE v0.1.0

<p align="center">
  <a href="https://github.com/Astral-Interactive-team/astral-remote/archive/refs/heads/main.zip">
    <img src="https://img.shields.io/badge/Download-Astral--Remote-8B5CF6?style=for-the-badge&logo=github&logoColor=white" alt="Download Astral Remote">
  </a>
  <a href="https://github.com/Astral-Interactive-team/astral-remote/stargazers">
    <img src="https://img.shields.io/github/stars/Astral-Interactive-team/astral-remote?color=8B5CF6&style=for-the-badge&label=%E2%98%85_Stars" alt="Star Astral Remote">
  </a>
  <a href="https://github.com/Astral-Interactive-team/astral-remote">
    <img src="https://img.shields.io/badge/Repo-astral--remote-181717?style=for-the-badge&logo=github&logoColor=white" alt="Official Repository">
  </a>
</p>

<p align="center">
  <b>A Windows system inventory agent that publishes a read-only dashboard to Discord.</b>
  <br>
  Developed by <b>Astral Interactive</b>.
</p>

---

> [!CAUTION]
> **Experimental software.** Use only on computers you own or are explicitly authorized to administer.
>
> The current production build embeds the Discord bot token in the executable as recoverable data. Anyone who obtains the EXE may be able to extract the token. Do not distribute a token-bearing executable to untrusted recipients.

## Overview

**Astral Remote** is a Python-based CLI application that builds a standalone Windows executable and connects it directly to Discord.

It combines a custom Rich terminal interface, Windows Agent packaging, and a read-only Discord system-information dashboard.

No separate Astral Remote backend server is required.

## Features

### Terminal Interface

Astral Remote provides a custom terminal interface with Astral Interactive branding.

| Page | Features |
|:--|:--|
| HOME | GitHub, Telegram, Discord |
| REMOTE | Generate Agent EXE, Start Agent |

The startup banner is stored in `astral_remote/cli/banner.txt`. The development CLI and production Agent use separate entry points.

### Discord Agent

The Agent connects to Discord with no commands of any kind: no prefix commands, no slash commands, and no command registration or synchronization. Its only Discord work is to create or adopt its managed per-computer category, publish the read-only system report to the managed `#info` channel, and keep that channel clean by deleting member messages posted there.

### System Information Dashboard

Each managed computer publishes a full system report to its managed `#info` channel using Discord **Components V2**. The report is read-only and is organized into a fixed set of topical messages, in this order: **Overview** (always first), **Windows & Operating System**, **Processor & Memory**, **Graphics & Firmware**, **Storage**, **Network & Power**, **Security & Updates**, and **Performance & Agent**. Each topic is published as its own message, so a reader can identify and open only the part they need; the topic heading is repeated at the top of every message, and a topic that outgrows one message continues in additional messages in order.

The Overview is a dashboard rather than a duplicate of the detail messages: it carries the computer identity, the Agent status, the operating system and Agent versions, the last update time, and the headline CPU, memory, and per-drive usage figures. Only the fixed local drives are listed there, and removable, network, and optical volumes are grouped into one line; full capacity, file-system, and BitLocker detail stays in the Storage topic.

The report deliberately contains **no process inventory, no service inventory, and no installed-software inventory**. It never lists running programs, process IDs, per-process CPU or memory, Windows services, installed applications, or the default browser.

There are no select menus, no pagination buttons, and no user-input controls. Every message opens with its title and position, `# Astral Remote — System Information (Part N of M)`, followed by its topic heading `## <topic>`. A topic that spans several messages simply repeats its heading; no continuation marker is ever printed, because the part number already carries the order. Nothing is summarized away or omitted, and facts the Agent cannot read are reported as `Unavailable` (or `Not supported` for features the platform does not expose) instead of being guessed. See [System Report](#system-report) for what is and is not collected.

### Executable Builder

Astral Remote packages its Agent using PyInstaller.

The build pipeline validates the configuration, prepares an isolated staging directory, packages the application, verifies the executable, and publishes the result only after validation succeeds.

Generated executable:

```text
dist/
└── astral-remote.exe
```

If the build fails verification, the previous executable is preserved.

## Requirements

| Dependency | Version |
|:--|:--|
| Python | 3.14 |
| Rich | 15.0.0 |
| discord.py | 2.7.1 |
| PyInstaller | 6.15.0 |
| pytest | 8.3.4 |

A Discord application and bot account are required for live Discord testing.

## Installation

### 1. Clone the repository

```powershell
git clone https://github.com/Astral-Interactive-team/astral-remote.git
cd astral-remote
```

Alternatively, download and extract the source ZIP from the download link above.

### 2. Create a virtual environment (recommended)

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

A virtual environment is recommended but not required.

### 3. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

The `astral_remote` package lives at the repository root, so `python -m astral_remote` runs directly with any Python 3.14 interpreter that has these dependencies, without installing the package. To also run it from other directories, install it editable:

```powershell
python -m pip install --no-deps -e .
```

### 4. Launch Astral Remote

```powershell
python -m astral_remote
```

Run the command from the repository root. No package installation, global installation, or PATH modification is required.

## Usage

Launch the interactive CLI:

```powershell
python -m astral_remote
```

Available menu actions:

| Option | Action |
|:--|:--|
| `01` | Generate Agent EXE |
| `02` | Start Agent |
| `N` | Open the REMOTE page |
| `B` | Return to HOME |
| `Q` | Exit |

Generating an Agent (option `01`, or `python scripts/build_exe.py`) prompts for:

| Prompt | Default | Effect |
|:--|:--|:--|
| Discord Bot Token | required | Embedded in the EXE as recoverable data. |
| Output EXE name | `astral-remote.exe` | Published under `dist/`. |
| Copy Agent into `%APPDATA%` | Yes | The EXE copies itself to `%APPDATA%\<name>\AstralRemote-Agent.exe` on launch so deleting the original keeps autostart working. |
| Register Windows autostart | Yes | The EXE registers `HKCU Run` and a logon task on launch. |

`<name>` is the output EXE name without its extension (for example `astral-remote`, or `MyAgent` for `MyAgent.exe`); the same name is used for the `%APPDATA%` folder, the `HKCU Run` value, and the logon task. Both install choices are embedded in the EXE at build time and are not asked on the target PC.

**Upgrading from a pre-rename build:** the Agent now installs itself as `%APPDATA%\AstralRemote\AstralRemote-Agent.exe` and registers the `AstralRemote` autostart entry. The old `AstralRelay` backup copy and its `HKCU Run` value and logon task are not removed automatically; delete them once so only one Agent runs. The existing `%LOCALAPPDATA%\AstralRelay\identity.json` is adopted automatically on first start (see below).

Display the version:

```powershell
python -m astral_remote --version
```

To invoke the standalone build script directly:

```powershell
python scripts/build_exe.py
```

The recommended workflow is to generate the Agent through the interactive menu.

## Build Pipeline

The build process performs four stages:

1. **Validation** — validate the token and runtime configuration.
2. **Preparation** — create an isolated staging directory and prepare the embedded payload.
3. **Packaging** — generate a Windows x64 executable using PyInstaller.
4. **Verification** — validate the output, executable subsystem, version command, and self-test result before publication.

The final output is:

```text
dist/
└── astral-remote.exe
```

The builder publishes the new executable only after verification succeeds and preserves the previous output when a build fails.

Each token-specific build currently requires a full PyInstaller rebuild, which can take several minutes.

## Discord Setup

The Python Agent exposes **no commands**: no prefix commands and no slash commands. It only manages its per-computer category and publishes the read-only system report.

The bot requires the **Message Content Intent** so it can receive and delete member messages posted in its managed `#info` channel.

Enable the intent in both:

1. The Agent's Discord intent configuration.
2. Discord Developer Portal → Application → Bot → Privileged Gateway Intents.

The bot also needs **Manage Channels** to create the managed category and its `#info` channel, and **Manage Messages** to clear member messages posted in `#info`.

### Access Control

There is nothing to authorize, because the Agent registers no commands and never acts on operator input.

Previously registered `/pc` slash commands and the old `!pc` prefix commands (`status`, `apps`, `open`, `lock`) have been removed from the codebase. Old slash registrations may remain visible in Discord until removed.

The Agent no longer ships a removal script. Remove old registrations manually from the **Discord Developer Portal** (Application → Commands) if they still appear. This is an explicit operator action and never runs automatically during startup.

### Managed Categories

Each Agent installation manages exactly one category per guild.

- A random, non-secret **installation ID** is generated once and stored at `%LOCALAPPDATA%\AstralRemote\identity.json`, beside the sanitized error log. An existing `%LOCALAPPDATA%\AstralRelay\identity.json` from a pre-rename build is copied into the new directory on first start, so the installation ID, managed category, and report message IDs are kept instead of creating a duplicate category.
- The category is named `<hostname> [<installation-id>]` and matched by that installation ID, so restarts, reconnects, and repeated `on_ready` events reuse the same category instead of creating more. The created category ID is remembered in the same file.
- Missing `Manage Channels` permission or Discord API failures warn once and are not retried in a loop.
- Categories left by older builds (`pc <hostname> (<host-id>)`) are adopted when unambiguous. Ambiguous or duplicate categories are reported to the file log and never deleted automatically.

Channels are added to the managed category one at a time, starting with `#info`:

- The bot creates a single `#info` text channel inside the category and publishes this computer's full system report there using Discord **Components V2** (no embeds, no select menus, no pagination). The channel ID and the ordered report message IDs are remembered in `identity.json`, so later starts refresh the same messages instead of posting duplicates.
- The report follows the fixed topic order above, starting with the Overview. Each message opens with `# Astral Remote — System Information (Part N of M)` and its topic heading `## <topic>`, so the order stays readable even when a topic spans several messages.
- Long lists are never summarized or truncated: every collected item is listed in full, and a list is split only at a section or item boundary, never mid-item.
- Process, service, and installed-software inventories are not collected or shown at all.
- Values the Agent genuinely cannot read are shown as `Unavailable`, and features the platform does not expose are shown as `Not supported`. No placeholder numbers are invented.
- Utilization at or above 80% is marked `- High` and at or above 90% `- Critical`, in text and in the message accent colour together.
- Member messages posted in `#info` are deleted immediately. The Agent registers no commands, so nothing posted anywhere is acted upon.

### System Report

The report is produced by two modules:

- `astral_remote/agent/windows/inventory.py` collects the facts with read-only registry reads, ctypes calls, and a single base64-encoded PowerShell CIM pass. Each probe is guarded, so a partial failure only marks its own fields `Unavailable`.
- `astral_remote/agent/info_report.py` renders, splits, and publishes the Components V2 messages.

Collected sections, in publication order:

1. **Overview** — hostname, installation ID, local and public IPv4, Agent status and version, operating system and version, last update time, CPU utilization, memory usage, per-drive usage and free space, and disk health. Fixed local volumes are listed one by one; removable, network, and optical volumes are grouped into a single line.
2. **Windows & Operating System** — edition and build (OS name, manufacturer, version, build number, feature update, edition, architecture, configuration, build type), Windows registration (registered owner and organization, Windows product ID, original install date), runtime (boot time, uptime, system and input locale, time zone), and domain and directories (domain/workgroup, domain joined, Windows and system directories, boot device, firmware architecture).
3. **Processor & Memory** — CPU manufacturer, model, identifier, architecture, physical and logical core counts, maximum and current clock speed, L2/L3 cache, virtualization firmware support, SLAT, hypervisor presence, utilization, and temperature; then total, used, and available physical memory, usage percentage, total and available virtual memory, page file size and usage, module count, and one line per installed module.
4. **Graphics & Firmware** — one bold block per graphics adapter (manufacturer, driver version and date, dedicated memory, resolution, refresh rate, HDR, adapter status) plus a single grouped `Monitoring: Not supported (...)` line naming each unsupported meter; then system and motherboard facts, BIOS version and release date, firmware mode, Secure Boot, and TPM availability, version, and firmware.
5. **Storage** — every logical volume (drive type, file system, label, total, used, free, used percentage), every physical drive (model, media type, interface, capacity, health, state, temperature, wear), and every BitLocker volume (protection, encryption state, method, percentage).
6. **Network & Power** — connectivity summary (internet reachability, public IPv4, adapter count, Discord gateway state and latency), one bold block per adapter (description, IPv4, IPv6, subnet mask, default gateway, DNS servers, DHCP state and server, MAC address, link speed, received and sent bytes), and power and battery facts.
7. **Security & Updates** — Windows Defender status, real-time protection, signature version and update time; one line per firewall profile (Domain, Private, Public); last installed update, hotfix count, and pending update state; UAC state and prompt configuration; and a single combined platform-security line for Secure Boot, TPM, and BitLocker.
8. **Performance & Agent** — live performance counters with their sampling window and timestamp, the Agent's own state (version, installation ID, uptime, Discord connection and latency, reconnect count, required management permission, managed category and channel state, duplicate-category warning, startup registration, backup executable, diagnostic-log summary), and Agent state-directory availability.

The layout uses only the three heading levels Discord renders: `#` for the message title, `##` for the message topic, and `###` for a section inside a topic, with `**Label:** value`, `**Item n: name**`, and `- bullet` lines for detail. Discord exposes no fourth heading level, so individual GPUs, memory modules, network adapters, and drives use bold item headers instead of a deeper heading.

Duplicate facts are kept in exactly one place: the Windows and system directories are reported once in the Windows & Operating System topic, the Secure Boot, TPM, and BitLocker details live in the firmware and storage topics with only a combined summary line under Security & Updates, and the Overview shows only the values whose full detail appears in a later topic.

Utilization that runs high is marked in words and in the message accent colour together, never by colour alone: at 80% or above a value is suffixed `- High`, and at 90% or above `- Critical`, which also switches the message's container accent to amber or red. The default accent colour is the Astral Remote purple.

The report collects **no process inventory, no service inventory, and no installed-software inventory**. Running programs, process IDs, per-process CPU or memory, Windows services, installed applications, Start Menu entry counts, and the default browser are neither read nor rendered.

What the collector deliberately does not read or send:

- No tokens, passwords, product keys, cookies, credentials, clipboard contents, keystrokes, audio, camera, screen captures, file contents, or process memory.
- Only the Windows **Product ID** is read; the product key is never touched.
- Disk, BitLocker, TPM, and Defender probes are read-only.
- Process command lines and environment-variable values are never read; the Agent environment section reports only which directories exist.

Every helper process the Agent starts (the PowerShell CIM pass, `schtasks`, and `taskkill`) is created with the Windows `CREATE_NO_WINDOW` flag. Because the production EXE is built with `--noconsole`, this is what keeps those helpers from flashing a console window.

The only outbound request is one short-timeout HTTPS lookup of the public IPv4 address (`api.ipify.org`, falling back to `icanhazip.com`). It sends no credentials and no identifiers beyond the HTTP request itself. If it fails, the field shows `Unavailable`.

Two fields are inferred rather than reported by Windows directly, and are documented here so they are not mistaken for first-hand facts:

- **BIOS mode** is inferred from the presence of the Secure Boot state registry key (`HKLM\SYSTEM\CurrentControlSet\Control\SecureBoot\State`): present means `UEFI`, otherwise `Legacy BIOS`.
- **Internet connectivity** comes from whether the public-IPv4 lookup succeeded; when the lookup fails but the endpoint is still reachable, the field says so explicitly instead of claiming full connectivity.


## Security

> [!WARNING]
> **The current executable contains a recoverable Discord bot token. Packaging is not encryption.**

The token is embedded in the production payload. Anyone with access to the generated executable may be able to extract it.

Recommended precautions:

- Use a dedicated Discord bot with the minimum required permissions.
- Never use a personal Discord account token.
- Avoid distributing an executable containing a sensitive bot token.
- Rotate the bot token if exposure is suspected.
- Never commit credentials or sensitive local configuration to Git.
- Review the Agent's automatic-start behavior before deployment.
- Run the Agent only on systems you own or are authorized to administer.

Windows persistence is chosen at build time. When enabled, the EXE copies itself into `%APPDATA%\<name>` (named after the output EXE without its extension) and registers automatic start through `HKCU Run` plus a logon task using the same name. Disable either the `%APPDATA%` copy or autostart while building if that behavior is not wanted.

The Agent runs without a visible console window. Understand these behaviors before launching it or distributing the executable.

**The current build should be treated as an experimental, locally controlled application rather than a secure binary-distribution format.**

## Testing

### Unit and integration tests

```powershell
python -m pytest tests/python/unit tests/python/integration -q
```

### Packaging tests

```powershell
$env:ASTRAL_RUN_PACKAGING = "1"
python -m pytest tests/python/packaging -q
```

The automated tests cover configuration validation, payload handling, per-installation category management, output redaction, and packaging behavior.

Tests using mocks or test credentials do not prove that live Discord authentication, all Windows operations, or every startup behavior works correctly.

## Known Limitations

- Live Discord login and category/report publishing require integration testing.
- Each token-specific build currently requires a full PyInstaller rebuild.
- The embedded bot token can be extracted from the executable.
- Automatic-start behavior should be reviewed before wider deployment.
- The system report is published only to managed `#info` channels; the Agent exposes no commands to fetch it on demand.
- The report deliberately omits process, service, and installed-software inventories.
- Utilization thresholds (`- High` at 80%, `- Critical` at 90%) and the matching accent colours are fixed constants, not user-configurable.
- Report updates happen when the Agent starts; deleting a report message makes the Agent repost it on the next start.
- The `CREATE_NO_WINDOW` flags for helper processes are covered by unit tests, but their effect has not been observed live from a rebuilt `--noconsole` EXE.

## Project Status

| Area | Status |
|:--|:--|
| Python CLI | Implemented |
| Terminal interface | Implemented |
| EXE packaging | Packaging tests passed |
| System report collection | Automated tests available |
| Live Discord integration | Pending verification |

Astral Remote remains experimental until live Discord integration and target-Windows operations have been verified.

## Project Structure

```text
astral-remote/
├── astral_remote/
│   ├── cli/
│   │   └── banner.txt
│   ├── shared/
│   ├── agent/
│   └── builder/
├── tests/
│   └── python/
│       ├── unit/
│       ├── integration/
│       └── packaging/
├── scripts/
│   └── build_exe.py
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Community

<p align="center">
  <a href="https://t.me/astral_int">
    <img src="https://img.shields.io/badge/Telegram-astral__int-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Astral Interactive on Telegram">
  </a>
  <a href="https://discord.gg/bSvcQG8K4f">
    <img src="https://img.shields.io/badge/Discord-Join_Community-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Astral Interactive Discord">
  </a>
</p>

<p align="center">
  <sub>Developed by Astral Interactive.</sub>
</p>