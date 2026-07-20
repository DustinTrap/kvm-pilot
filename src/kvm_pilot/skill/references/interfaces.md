# Choosing an interface — best tool per action

> Part of the bundled kvm-pilot skill. Re-read this before picking how to do an
> action you haven't done this session — the right interface is chosen per
> *action*, not per session. Also served at runtime by the MCP `doctrine` tool
> (topic "interfaces").

kvm-pilot is reachable through several interfaces, and **no single one is best
for everything.** Pick per action, and run more than one at once when the work
is independent (see [Multitasking](#multitasking--use-interfaces-in-parallel)).
This is the operator-side complement to the sensing hierarchy (#13, prefer
structured/text over vision) and the actuation-channel hierarchy (#81, hand off
KVM HID+vision → SSH once the target OS is reachable — see
[recovery.md](recovery.md)).

| Action | Best interface | Notes / fallback |
|---|---|---|
| See the screen as a model-visible image | **MCP** `snapshot` | Returns a real image content block — no screenshot-file round-trip. CLI `snapshot` writes a file. The JSON payload carries `signal` (`hdmi_signal` — the authoritative picture-present flag — plus online/resolution/fps/`streamer_idle`) and `unchanged_since_last_snapshot` — a byte-identical frame when the screen should have changed means stale/cached pixels: check `signal` + `logs` before trusting or acting on it. |
| Classify boot/run phase | **MCP** `classify_screen` | Uses the server's vision backend if configured; **with no server key it falls back to caller-side** — hands you the screenshot + prompt to classify yourself (a `[json, image]` result). CLI: `classify` / `watch`. |
| Wait for a boot/run phase | **MCP** `wait_for_state` or CLI `watch` | Bounded server-side wait (≤ 300 s per call — chain calls for long installs; the timeout result carries the last observed state). Success returns the final `frame_ref` for a follow-up `mouse` click. Phases the cheap gates can't resolve need server-side vision — with no server key it errors fast: poll `classify_screen` (caller-side) instead. |
| Preflight audit (run first) | **MCP** `healthcheck` or CLI `healthcheck` | The intake gate — run it on first contact (see [../SKILL.md](../SKILL.md)). |
| Device info / host power state | **MCP** `info` / `power_state`, or CLI | Either works. |
| List what the driver supports | **MCP** `capabilities` or CLI `capabilities` | Structural/offline — no network, no preflight. Use it to pick the right interface up front. |
| **Read the device/host event log** | **MCP** `logs` or **CLI `logs`** | The text diagnostic when video/streamer/power looks wrong — it names a fault (e.g. a stuck encoder behind a `snapshot` 503) a screenshot can't. |
| Type / press a key / send a shortcut on the host console | **MCP** `type_text` / `press_key` / `send_shortcut` / `ctrl_alt_delete`, or CLI `type` / `key` | HID input, gated by effect: needs `KVM_PILOT_MCP_ALLOW_HID` + per-call approval; a reboot chord (Ctrl+Alt+Del, SysRq) needs `ALLOW_POWER`. |
| Move / click the mouse (installers, BIOS, desktops) | **MCP** `mouse` | Absolute positioning; `percent` coords by default. A click must carry `observed_frame_ref` from a recent `snapshot` (refused if the host rebooted since). Needs `KVM_PILOT_MCP_ALLOW_HID`. |
| See what media is already on the KVM | **MCP** `list_virtual_media` or CLI `media-list` | Read-only MSD inventory. **Check this before asking the user to download or upload an ISO** — the image may already be in storage from an earlier install; mount it instead of round-tripping gigabytes. The reply's `host_visible_as` (when present) is the device name the target's boot menu shows for truly presented media — match it to confirm readiness and to pick the correct boot entry instead of guessing. |
| Mount / eject install media | **MCP** `mount_iso` / `eject`, or CLI `mount` / `eject` | Virtual media (ISO path or URL). MCP needs `KVM_PILOT_MCP_ALLOW_MEDIA` + approval. |
| **Set the next boot device** (PXE/CD/HDD/BIOS) | **MCP `set_boot_device`** (gated CONFIG) or CLI `boot-device` | Redfish / IPMI / AMT. AMT is **single-use only** and its source override is **write-only** — confirm it by watching the boot, not by reading it back. |
| **Screenshot BIOS/POST/GRUB on a laptop** the capture-KVM can't see boot | **`snapshot` with `--driver amt`** (MCP or CLI) | Intel AMT renders the firmware framebuffer *below* the OS — the one driver that sees pre-boot on a laptop. **Graphical screens only** (not legacy VGA text mode; a reset right after the request means "unsupported display mode"). |
| **Attach a text serial console (SOL)** | **CLI `console`** (IPMI or AMT) | Watch BIOS/GRUB/kernel over serial when serial-redirect is on. No MCP serial tool. |
| **Enable AMT SOL/KVM redirection** (open the listeners) | **CLI `amt enable-sol` / `enable-kvm`**, or **MCP `amt_enable`** (gated CONFIG) | Over WS-Man, no MEBx trip. `--no-consent` (CLI) / `consent_off` (MCP, needs the extra `ALLOW_CONSENT_OFF` gate) disables the on-screen user-consent prompt. Clear a wedged single KVM session with `amt reset-kvm`. |
| Watch typed device events (atx/msd/streamer changes) | **MCP** `events` (bounded collect) or CLI `events` (follow mode) | The text cross-check for a vision wait (#233). Follow-mode streaming is CLI-only. |
| Check firmware currency vs the registry | **MCP** `firmware_check` (read-only) or CLI `firmware-check` | The CLI additionally auto-files the registry report; the MCP filing twin is `file_firmware_report` (gated external write). |
| firmware-update | **CLI only** | The MCP server does not expose it. |
| Contribute firmware currency to the registry (file the report) | **MCP `file_firmware_report`** or CLI `firmware-check` (auto-files) | Files a GitHub issue via the `gh` CLI when the registry is behind — an external write: MCP needs `KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE` + approval; `dry_run=true` previews (#190). |
| MSD mode switching | **Python library only** | Not in MCP or CLI. |
| Change **host** power (on/off/cycle/reset) | **MCP `power`** (gated) or CLI `power` / `power-cycle` | Destructive — confirm each step. MCP `power` is operator-enabled + per-call approval. |
| Reboot the **KVM appliance** (clear a wedged encoder) | **MCP `appliance_reboot`** (gated `KVM_PILOT_MCP_ALLOW_APPLIANCE` + confirm) or SSH to the appliance | Drops KVM control ~60 s; target power untouched — never automate it. Restarting just `kvmd` or inspecting `/etc/kvmd` is still SSH-to-the-appliance only. |
| Check if the **target host** is reachable / run commands on it once its OS is up | **MCP `ssh_reachable` / `ssh_exec`**, or CLI `ssh-check` / `ssh-exec` (in-band) | Prefer SSH over KVM keystrokes once the OS is up. Configure the target's IP/host/FQDN via `ssh_host` (≠ the KVM's address); `ssh_exec` is gated (operator opt-in `KVM_PILOT_MCP_ALLOW_SSH`). See [recovery.md](recovery.md). |
| Bootstrap SSH during an install (set up the cheap channel over the expensive one) | **CLI `ssh-bootstrap`** | Once an installer is up, switches to a text console, reads the DHCP IP off the screen, starts `sshd`, and hands off to SSH. **Plans by default** — pass `--execute` to run it; add a `--command` that installs a key/password for a usable channel. Guided/conservative (aborts if the console can't be confirmed); not an MCP tool. |
| View the screen when `snapshot` fails | **WebRTC/Janus stream or the vendor web UI** | The only way to see a unit that streams H.264 at its native resolution. |

## Multitasking — use interfaces in parallel

The interfaces don't contend; run independent work concurrently to cut latency
and cross-check signals:

- **Parallel intake.** Gather `healthcheck` + `info` + `capabilities` + `logs`
  (+ `firmware-check`) at once rather than serially.
- **Cross-signal during long waits.** While a vision wait (`wait_for_state` /
  CLI `watch`) waits for a boot
  phase, tail `logs`/`events` alongside it, so a text signal can confirm or
  contradict the pixel read (the operator-side of #13's sensing hierarchy).
- **Mix channels.** The in-session MCP image path and a CLI `events`/`logs`
  stream can run together — different transports, no conflict.
- **Never parallelize state changes.** Serialize anything destructive (power,
  media, keystrokes) behind a single confirm gate; concurrency is for read-only
  observation only.

## CLI

The CLI covers the **full surface** and is the only interface for
`firmware-check`/`firmware-update`, `events`, and `ssh-bootstrap`
(see the interface matrix above — `watch` has an MCP twin,
`wait_for_state`, and keyboard/mouse/media/boot-config all have MCP act
tools). Use the MCP server for the visual loop (`snapshot`/`classify`)
and the gated act/power tools inside an agent session; use the CLI for
everything else and for one-off checks when no MCP host is in the loop.

The full command set (see [docs/cli.md](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/cli.md)
for the reference table): `kvm-pilot info | capabilities | benchmark | route |
host-exec | healthcheck | firmware-check | firmware-update | snapshot | sensors |
logs | ssh-check | ssh-exec | ssh-discover | ssh-bootstrap | boot-progress |
power | power-cycle | boot-device | wake | console | type | key | mouse-move | click | calibrate-mouse | media-list | mount |
eject | keep-awake | recover-hid | appliance | paths | classify | watch |
events | test-report`. Run
`healthcheck` on first contact (see [../SKILL.md](../SKILL.md)); it also auto-runs ahead of destructive
subcommands. `firmware-check` reports firmware currency and, where a device knows
its vendor's latest, **auto-files** the registry contribution as a GitHub issue
(needs the `gh` CLI; `--no-file-report` to opt out, `--dry-run` to preview).
`test-report` probes the device and appends an evidence row to the run ledger
(#99) — read-only probes always; destructive only via `--include` + `--attest`.
`--dry-run` logs destructive
actions without sending them (it short-circuits before any prompt, so it is
safe in automation); `--yes` skips the interactive y/N confirmation on a real
run. See `kvm-pilot --help`.
