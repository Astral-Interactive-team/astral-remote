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
> Builds contain no Discord bot token. Configure each eligible Host PC locally with `astral-remote credentials set` before starting Host work.

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

The Agent connects to Discord with one read-only prefix command, `status`, available only in its managed host `#main` channel; it registers no slash commands and no command tree. Its Discord work is to create or adopt its managed per-computer category with its `#info` and `#main` channels, ensure the server-wide `control` category with its `#main` and `#logs` channels, publish the read-only system report to the managed `#info` channel, keep that channel clean by deleting member messages posted there, and answer `status` in the host `#main` with a short summary for that category's computer.

### System Information Dashboard

Each managed computer publishes a full system report to its managed `#info` channel using Discord **Components V2**. The report is read-only and is organized into a fixed set of topical messages, in this order: **Overview** (always first), **Windows & Operating System**, **Processor & Memory**, **Graphics & Firmware**, **Storage**, **Network & Power**, **Security & Updates**, and **Performance & Agent**. Each topic is published as its own message, so a reader can identify and open only the part they need; the topic heading is repeated at the top of every message, and a topic that outgrows one message continues in additional messages in order.

The Overview is a dashboard rather than a duplicate of the detail messages: it opens with the live Discord connection banner (`🟢 Connected`, `🔴 Disconnected`, or `🟡 Unknown`), then carries the computer identity, OS and Agent versions, the last update time, and the headline CPU, memory, and per-drive usage figures. Local and public IPv4 appear as separate quoted, inline-code lines; a failed public lookup shows `Unavailable` and is never replaced with the local address. Only the fixed local drives are listed there, and removable, network, and optical volumes are grouped into one line; full capacity, file-system, and BitLocker detail stays in the Storage topic. The connection banner reflects the Discord Gateway link only, never a judgement that the whole PC is healthy.

The report deliberately contains **no process inventory, no service inventory, and no installed-software inventory**. It never lists running programs, process IDs, per-process CPU or memory, Windows services, installed applications, or the default browser.

There are no select menus, no pagination buttons, and no user-input controls. Every message opens with its title and position, `# Astral Remote — System Information (Part N of M)`, followed by its topic heading `## <topic>`. A topic that spans several messages simply repeats its heading; no continuation marker is ever printed, because the part number already carries the order. Identifiers such as the installation ID, hostname, Agent version, drive letters, MAC address, and state paths use inline code, while only identity-worthy values such as the Overview IP addresses are quoted. Nothing is summarized away or omitted, and facts the Agent cannot read are reported as `Unavailable` (or `Not supported` for features the platform does not expose) instead of being guessed. See [System Report](#system-report) for what is and is not collected.

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
| `Q` | Exit (`Type Q again to quit:`; Ctrl+C asks for a second Ctrl+C) |

Generating an Agent (option `01`, or `python scripts/build_exe.py`) prompts for:

| Prompt | Default | Effect |
|:--|:--|:--|
| Discord Bot Token | required for Host only | Stored locally with current-user Windows DPAPI; never embedded in EXE. |
| Output EXE name | `astral-remote.exe` | Published under `dist/`. |
| Bot command prefix | `!` | Embedded in the EXE as `prefix` for the managed `#main` command channel. 1–8 characters, no spaces. |
| Copy Agent into `%APPDATA%` | Yes | The EXE copies itself to `%APPDATA%\<name>\AstralRemote-Agent.exe` on launch so deleting the original keeps autostart working. |
| Register Windows autostart | Yes | The EXE registers `HKCU Run` and a logon task on launch. |

`<name>` is the output EXE name without its extension (for example `astral-remote`, or `MyAgent` for `MyAgent.exe`); the same name is used for the `%APPDATA%` folder, the `HKCU Run` value, and the logon task. Both install choices and the command prefix are embedded in the EXE at build time and are not asked on the target PC.

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

One EXE may be deployed to multiple PCs. Only PCs explicitly configured with a local Host credential can start Discord Host work.

## Discord Setup

The Python Agent exposes one command: the read-only `status` prefix command in its managed `#main` channel. There are no slash commands and no other prefix commands. #info accepts no commands.

The bot requires the **Message Content Intent** so it can receive and delete member messages posted in its managed `#info` channel.

Enable the intent in both:

1. The Agent's Discord intent configuration.
2. Discord Developer Portal → Application → Bot → Privileged Gateway Intents.

The bot also needs **Manage Channels** to create the managed host category and its `#info` and `#main` channels, the server-wide `control` category and its `#main` and `#logs` channels, and **Manage Messages** to clear member messages posted in `#info`.

### Commands

Command availability is decided per managed channel:

| Channel | Available commands |
|:--|:--|
| `#main` | `status` |
| `#info` | none (member messages are deleted) |
| Any other channel | none |

The command prefix is the value chosen at build time (default `!`), so an EXE built with the default answers `!status`. The command word is case-insensitive and extra arguments are ignored; an unknown command is ignored silently.

`status` answers with a plain channel message whose content is one grouped code block, sent for the computer that owns the `#main` channel where it was posted. The reply opens with `ASTRAL REMOTE — STATUS` and is split into `[ AGENT ]` (state, hostname, installation ID, version, uptime), `[ CONNECTION ]` (Discord state, gateway latency, reconnect count), and `[ LAST UPDATED ]` (the time the reply was generated). General command replies are plain fenced code blocks with no Components V2 wrapper, so labels and values line up in monospace. It does not repeat the full #info report, collects nothing new, changes no system state, and echoes no user input. Because each Agent answers only in its own managed `#main`, a guild with several managed categories gets a different answer from each one.

### Access Control

Anyone who can post in a managed `#main` channel can run `status`. That is acceptable because the command is read-only and reveals only facts already published in `#info` beside it. Posting in `#info` has no effect: those messages are deleted.

Previously registered `/pc` slash commands and the old `!pc` prefix commands (`apps`, `open`, `lock`) have been removed from the codebase. The only current command is the read-only `status` described above. Old slash registrations may remain visible in Discord until removed.

The Agent no longer ships a removal script. Remove old registrations manually from the **Discord Developer Portal** (Application → Commands) if they still appear. This is an explicit operator action and never runs automatically during startup.

### Managed Categories

Each Agent installation manages exactly one host category per guild, and ensures one shared `control` category per guild.

#### Host category

The host category is owned by one installation:

- A random, non-secret **installation ID** is generated once and stored at `%LOCALAPPDATA%\AstralRemote\identity.json`, beside the sanitized error log. An existing `%LOCALAPPDATA%\AstralRelay\identity.json` from a pre-rename build is copied into the new directory on first start, so the installation ID, managed category, and report message IDs are kept instead of creating a duplicate category.
- The category is named `<hostname> [<installation-id>]` and matched by that installation ID, so restarts, reconnects, and repeated `on_ready` events reuse the same category instead of creating more. The created category ID is remembered in the same file.
- Missing `Manage Channels` permission or Discord API failures warn once and are not retried in a loop.
- Categories left by older builds (`pc <hostname> (<host-id>)`) are adopted when unambiguous. Ambiguous or duplicate categories are reported to the file log and never deleted automatically.

#### Control category

On every start the Agent also ensures exactly one server-wide `control` category per guild, independent of the host category. Control setup starts before host setup and runs in parallel with it, so the category appears while the first report is still being collected and published. It is matched by its remembered ID first and then by the exact category name `control`, so repeated starts and several Agents on the same server reuse it instead of creating duplicates.

- The category is created when missing; `#main` and `#logs` are then added in that order. Existing channels with those names are adopted, and the category plus both channel IDs are remembered in `identity.json`.
- These channels are scaffolding for later work: nothing is written to `#logs`, and the control `#main` accepts no commands yet. The host category's `#main` remains the `status` command channel.
- The control channels are not refreshed, cleaned, or used for the system report.

Channels are added to the host category one at a time, starting with `#info`:

- The bot creates a single `#info` text channel inside the category and publishes this computer's full system report there using Discord **Components V2** (no embeds, no select menus, no pagination). The channel ID and the ordered report message IDs are remembered in `identity.json`, so later starts refresh the same messages instead of posting duplicates. While the Agent stays connected, the same messages are refreshed every 60 seconds, so utilization, uptime, and the `Last updated` timestamp stay current without adding new messages.
- The bot then creates a single `#main` text channel directly below `#info`; its channel ID is remembered in the same file. `#main` is the operator command channel: it accepts `status`, while the Agent publishes nothing there and never deletes messages posted there. An existing `#main` channel in the category is adopted instead of duplicated. The command prefix is chosen at build time (default `!`) and stays embedded in the EXE as `prefix`.
- The report follows the fixed topic order above, starting with the Overview. Each message opens with `# Astral Remote — System Information (Part N of M)` and its topic heading `## <topic>`, so the order stays readable even when a topic spans several messages.
- Long lists are never summarized or truncated: every collected item is listed in full, and a list is split only at a section or item boundary, never mid-item.
- Process, service, and installed-software inventories are not collected or shown at all.
- Values the Agent genuinely cannot read are shown as `Unavailable`, and features the platform does not expose are shown as `Not supported`. No placeholder numbers are invented.
- Utilization below 80% is marked `- 🟢 **Healthy**`, at or above 80% `- 🟡 **High**`, and at or above 90% `- 🔴 **Critical**`, in text, status icon, and the message accent colour together.
- Member messages posted in `#info` are deleted immediately; the host `#main` accepts only the `status` command, and any other message there is ignored. The control `#main` and `#logs` are inert.

### System Report

The report is produced by two modules:

- `astral_remote/agent/windows/inventory.py` collects the facts with read-only registry reads, ctypes calls, and a single base64-encoded PowerShell CIM pass. Each probe is guarded, so a partial failure only marks its own fields `Unavailable`.
- `astral_remote/agent/info_report.py` renders, splits, and publishes the Components V2 messages.

Collected sections, in publication order:

1. **Overview** — live Discord connection state, hostname, installation ID, local and public IPv4, operating system and version, Agent version, last update time, CPU utilization, memory usage, per-drive usage and free space, and disk health. Fixed local volumes are listed one by one; removable, network, and optical volumes are grouped into a single line.
2. **Windows & Operating System** — edition and build (OS name, manufacturer, version, build number, feature update, edition, architecture, configuration, build type), Windows registration (registered owner and organization, Windows product ID, original install date), runtime (boot time, uptime, system and input locale, time zone), and domain and directories (domain/workgroup, domain joined, Windows and system directories, boot device, firmware architecture).
3. **Processor & Memory** — CPU manufacturer, model, identifier, architecture, physical and logical core counts, maximum and current clock speed, L2/L3 cache, virtualization firmware support, SLAT, hypervisor presence, utilization, and temperature; then total, used, and available physical memory, usage percentage, total and available virtual memory, page file size and usage, module count, and one line per installed module.
4. **Graphics & Firmware** — one bold block per graphics adapter (manufacturer, driver version and date, dedicated memory, resolution, refresh rate, HDR, adapter status) plus a single grouped `Monitoring: Not supported (...)` line naming each unsupported meter; then system and motherboard facts, BIOS version and release date, firmware mode, Secure Boot, and TPM availability, version, and firmware.
5. **Storage** — every logical volume (drive type, file system, label, total, used, free, used percentage), every physical drive (model, media type, interface, capacity, health, state, temperature, wear), and every BitLocker volume (protection, encryption state, method, percentage).
6. **Network & Power** — connectivity summary (internet reachability, public IPv4, adapter count, Discord gateway state and latency), one bold block per adapter (description, IPv4, IPv6, subnet mask, default gateway, DNS servers, DHCP state and server, MAC address, link speed, received and sent bytes), and power and battery facts.
7. **Security & Updates** — Windows Defender status, real-time protection, signature version and update time; one line per firewall profile (Domain, Private, Public); last installed update, hotfix count, and pending update state; UAC state and prompt configuration; and a single combined platform-security line for Secure Boot, TPM, and BitLocker.
8. **Performance & Agent** — live performance counters with their sampling window and timestamp, the Agent's own state (version, installation ID, uptime, Discord connection and latency, reconnect count, required management permission, managed category and channel state, duplicate-category warning, startup registration, backup executable, diagnostic-log summary), and Agent state-directory availability.

The layout uses only the three heading levels Discord renders: `#` for the message title, `##` for the message topic, and `###` for a section inside a topic, with `**Label:** value`, `**Item n: name**`, and `- bullet` lines for detail. Discord exposes no fourth heading level, so individual GPUs, memory modules, network adapters, and drives use bold item headers instead of a deeper heading.

Duplicate facts are kept in exactly one place: the Windows and system directories are reported once in the Windows & Operating System topic, the Secure Boot, TPM, and BitLocker details live in the firmware and storage topics with only a combined summary line under Security & Updates, and the Overview shows only the values whose full detail appears in a later topic.

Utilization is marked in words, a status icon, and the message accent colour together, never by colour alone: a measured value below 80% ends in `- 🟢 **Healthy**`, at 80% or above it ends in `- 🟡 **High**`, and at 90% or above in `- 🔴 **Critical**`, which also switches the message's container accent to amber or red. A measurement that cannot be read gets neither a number nor a status icon. Disk health uses the same vocabulary (`🟢 Healthy`, `🟡 Warning`, `🔴 Unhealthy`) and the default accent colour is the Astral Remote purple.

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
> **Current-user DPAPI protects local Host credentials; it does not protect a compromised process or Windows user account.**

Build payload contains only non-secret configuration. `astral-remote credentials set` reads token with masked console input and writes `%LOCALAPPDATA%\AstralRemote\host-credential.json`; its content is DPAPI-encrypted for current Windows user. `credentials clear` removes it. A Host candidate without this local credential remains an Agent and Host start fails without a Discord connection.

`astral-remote identity show` creates or displays local Ed25519 Node Identity. Private key is held in `%LOCALAPPDATA%\AstralRemote\node-identity.json` inside current-user DPAPI protected envelope; public key, `node-v1-<sha256>` identifier, key algorithm, and format versions are independent from legacy non-secret Installation ID in `identity.json`.

Future peer enrollment must authenticate possession of Node Identity key, then record its public key in trust policy. Installation ID alone never establishes trust. This release has no peer handshake, discovery, automatic enrollment, or credential distribution.

## Peer Network (Phase 03)

`peerNetwork` is optional non-secret runtime configuration. It binds only configured address (default loopback), then attempts configured bootstrap peers. No default public peer exists.

```json
{"peerNetwork":{"enabled":true,"bindHost":"127.0.0.1","bindPort":9443,"bootstrapPeers":[{"nodeId":"node-v1-...","publicKey":"base64-ed25519-public-key","host":"192.0.2.10","port":9443,"protocolVersion":1}]}}
```

Each bootstrap key must also be registered in local `TrustStore`; address presence never grants trust. Transport is direct TLS 1.3 with signed Ed25519 nonce handshake bound to protocol/network/version and both session nonces. Unknown, revoked, malformed, replayed, or version-mismatched peers are rejected. No remote commands, token transport, auto-enrollment, peer discovery, HTTP/WebSocket API, DHT, NAT traversal, relay, UPnP, or firewall change exists in this phase. Direct TCP/TLS therefore needs reachable configured endpoints; NAT/Internet behavior is unverified.

Recommended precautions:

- Use a dedicated Discord bot with the minimum required permissions.
- Never use a personal Discord account token.
- Configure Host credentials separately on every authorized Host candidate.
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
- Existing token-bearing EXEs should be treated as exposed; rotate that Discord token and rebuild with current code.
- Automatic-start behavior should be reviewed before wider deployment.
- The system report is published only to managed `#info` channels; `status` in the host `#main` returns a short summary, not the full report.
- The server-wide `control` category and its `#main` and `#logs` channels are created at startup but have no command or logging behavior yet.
- The report deliberately omits process, service, and installed-software inventories.
- Utilization thresholds (`- 🟢 **Healthy**` below 80%, `- 🟡 **High**` at 80%, `- 🔴 **Critical**` at 90%) and the matching accent colours are fixed constants, not user-configurable.
- Report updates happen when the Agent starts and every 60 seconds while it stays connected; deleting a report message makes the Agent repost it on the next refresh.
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
