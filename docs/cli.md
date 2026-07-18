# CLI reference

Every `kvm-pilot` subcommand in one table. This is the canonical list — if a
command exists in `cli.py` it must appear here (`kvm-pilot --help` is the
runtime source of truth for flags). Global flags precede the subcommand:
`--version`, `-v/--verbose`, `--timeout SECONDS` (HTTP per-request timeout).

Destructive commands (marked ⚡) auto-run the preflight healthcheck first and
prompt for confirmation; `--dry-run` logs instead of sending, `--yes` skips the
prompt on a real run. Capability column = what the device's driver must
support (`kvm-pilot capabilities` lists them, offline).

| Command | ⚡ | Capability | What it does |
|---|---|---|---|
| `info` | | system_info | Device/system info as JSON. |
| `capabilities` | | — (offline) | List the capabilities this driver supports; `--json` for an array. |
| `benchmark` | `--samples`, `--no-hid`, `--no-os-plane`, `--select CMD`, `--save`, `--json` | — | Profile per-command latency + capability across interfaces (library/ssh/winrm) → the adaptive router scorecard (#181). |
| `route` | `<command>`, `--fresh`, `--samples`, `--no-os-plane`, `--json` | — | Print the interface the router picks for a command (uses/refreshes the cached per-device scorecard) (#181). |
| `host-exec` | `<cmd>`, `--powershell`, `--shell` | — | Run a command on the managed host's OS via the fastest capable **in-band** interface (ssh/winrm), auto-selected + self-tuned (#181). |
| `healthcheck` | | — | Preflight audit (readiness/security/firmware, #80). Run on first contact. `--json`, `--fix` (offer safe reversible auto-fixes). |
| `firmware-check` | | system_info | Firmware currency vs the bundled registry; auto-files the latest-known report upstream when the registry is behind (#189). `--no-file-report`, `--source URL`, `--date`, `--repo`, `--dry-run` (preview the issue body). |
| `firmware-update` | ⚡ | firmware_update | Assess and (with `--execute`) perform a gated remote flash. Plans by default; verifies the device actually entered an upgrade state (#94). `--image`, `--i-have-physical-access`. |
| `test-report` | ⚡ (only with `--include`) | — | Probe the device's capabilities and append one evidence row to the run ledger (#99; automates the docs/test-plan.md §9 intake). Read-only probes (info, snapshot+conditions, healthcheck, logs, power_state) always run; destructive ones (`--include virtual_media,power,firmware_update`) additionally need `--attest "<operator statement>"` (recorded on the row) and still go through the normal safety gates — **the power probe genuinely power-cycles the target** on working hardware. Pass = assertion + observed effect; FAILs are recorded, not raised. `--iso`, `--image`, `--ledger PATH` (default `$KVM_PILOT_TEST_LEDGER`, else `~/.config/kvm-pilot/test_runs.jsonl` — never the installed package data), `--synthetic`, `--json`. |
| `snapshot` | | video | Save a screenshot to a file (validated JPEG, #107). |
| `sensors` | | sensors | Structured sensors (temps/fans/power/voltages) — BMC drivers. |
| `logs` | | logs | Device/host event log; `--seek N` = seconds of lookback. |
| `boot-progress` | | boot_progress | Structured boot phase (BMC BootProgress). |
| `console` | ⚡ | serial_console | Attach an interactive serial console (IPMI or AMT SOL) to your terminal; exit with `~.` (tilde, period). Text-only — needs BIOS console redirection / a serial getty on the host (#208). |
| `ssh-check` | | — (ssh_host) | Is the managed host's OS reachable over SSH (in-band)? |
| `ssh-exec` | ⚡ | — (ssh_host) | Run a command on the managed host's OS over SSH (gated). |
| `ssh-discover` | | — | Scan a CIDR for open SSH. RISKY/opt-in — your networks only. `--ssh-port`. |
| `ssh-bootstrap` | ⚡ | hid, video | Bootstrap SSH on an installer host over KVM HID, then hand off (#81). Plans by default; `--execute`, `--vt`, `--command` (repeatable), `--ip-region`. |
| `power` | ⚡ | power | `on` / `off` / `off-hard` / `reset`. |
| `power-cycle` | ⚡ | power | Hard power cycle (off-hard → on). |
| `boot-device` | ⚡ | boot_config | Set what the host boots next (`pxe`/`cd`/`hdd`/`usb`/`bios`/`diag`/`none`) via Redfish `BootSourceOverride`, IPMI, or AMT (AMT is single-use only — `--persistent` is rejected); `--once`/`--persistent`, `--legacy` (BIOS vs UEFI), `--show` (read current + allowable), `--via {auto,redfish,ssh}` (ssh sets a one-time UEFI `BootNext` via `efibootmgr`) (#28/#201/#150). |
| `wake` | ⚡ | — (mac) | Send a Wake-on-LAN magic packet to the host's `mac` (from `--mac` or the profile); the OOB power-on path when the KVM has no wired ATX/GPIO (#199/#23). |
| `type` | ⚡ | hid | Type text on the host console; `--slow` for finicky firmware. |
| `key` | ⚡ | hid | Press a key (`Enter`, `F2`) or send a chord of kvmd key codes (`ControlLeft+AltLeft+F2`, #112). |
| `mouse-move` | ⚡ | hid | Absolute mouse move; `--space percent` (default, 0.0–1.0, resolution-proof) \| `pixel` \| `raw` (#124). Percent coords apply the stored per-host calibration when present (#128) and say so. |
| `click` | ⚡ | hid | Mouse click (`left`/`right`/`middle`); `--at X Y` moves first, `--double` (#124). `--at` percent coords apply the stored calibration (#128). |
| `calibrate-mouse` | ⚡ | hid, video | Measure & store this host's commanded→observed mouse correction (#128): park → 5-point grid → least-squares fit → held-out verify (`--tolerance`, default 0.02 of the screen). Moves the live cursor ~10–30s on a **static** screen; needs Pillow (`pip install 'kvm-pilot[calibrate]'`). Stored per (host, capture resolution); a resolution change makes it stale (never applied). |
| `keep-awake` | | hid | Toggle the on-device mouse jiggler (`on`/`off`) so the target display doesn't DPMS-sleep between steps (#159). |
| `recover-hid` | | hid | Reset/re-enumerate the USB HID gadget when keystrokes aren't reaching the target (#160). |
| `appliance` | ⚡ (reboot) | — (appliance_ssh) | The KVM appliance's **own** OS (#162): `loadavg` (read-only diagnostics) or `reboot` (gated recovery; drops KVM control ~60s, target power untouched). |
| `amt` | ⚡ | — (driver amt) | Intel AMT/vPro redirection enablement over WS-Man (needs `--driver amt`), no MEBx trip: `enable-sol` opens the SOL listener (16994); `enable-kvm` opens KVM redirection (5900) + sets the 8-char RFB password (`--no-consent` disables the on-screen user-consent prompt — Admin Control Mode only, CLI-only); `reset-kvm` clears a wedged single KVM session. |
| `paths` | | — | Show which independent recovery paths are live (kvmd-REST / appliance-SSH / target-SSH / OOB power / console-HID), labeled by failure domain — the lockout-exposure view (#162). `--json`. |
| `media-list` | | virtual_media | List images already on the KVM's MSD storage — check before downloading/uploading an ISO (#127). |
| `mount` | ⚡ | virtual_media | Mount an ISO (local path or URL); verifies the media actually reports online (#77). `--name`, `--usb`. |
| `eject` | ⚡ | virtual_media | Detach virtual media (inverse of `mount`). |
| `classify` | | video | Classify the current screen into a boot/run phase once (vision backend flags: `--backend`, `--vision-url`, `--vision-model`, `--hint`). |
| `watch` | | video | Wait until the screen reaches a phase; `--timeout` is the vision deadline (distinct from the global `--timeout`). |
| `events` | | events | Stream device events (WebSocket; `websocket-client` is bundled as a base dep); `--duration`, `--count`, `--no-stream`. |

Common selection flags on device commands: `--profile NAME` (config-file
profile), `--host/--user/--passwd/--port/--scheme`, `--driver`
(`pikvm`/`glkvm`/`blikvm`/`redfish`/`ipmi`/`amt`/`fake`), plus the `KVM_PILOT_*`
environment variables ([configuration reference](configuration.md)).

See also: the [MCP server tool table](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/mcp/README.md)
for the agent-facing surface, and the interface matrix in the bundled skill
for which interface to prefer per action.
