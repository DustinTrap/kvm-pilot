# Troubleshooting & FAQ

Symptom-first fixes for the failures people actually hit. Each section is the
canonical home for its topic — other docs link here rather than restating it.

| Symptom | Jump to |
|---|---|
| Every `/api/*` call returns 404 (GLKVM) | [API disabled](#every-api-call-returns-404-glkvm) |
| `snapshot` 503s, returns garbage, or shows black | [Snapshot failures](#snapshot-fails-or-lies) |
| Act tools return `approval cancel` / `denied by approver` | [Approval delivery](#act-tools-denied-approval-cancel--denied-by-approver) |
| Host went dark — no video, no HID, no ping | [Dark host](#the-host-went-dark-no-video-no-hid-no-ping) |
| Mouse clicks land in the wrong place | [Calibration](#mouse-clicks-land-in-the-wrong-place) |
| SOL serial console prints binary noise (iDRAC6) | [SOL on COM2](#sol-serial-console-shows-binary-noise-idrac6) |
| `pip install kvm-pilot` installs nothing useful | [Install](#pip-install-kvm-pilot-doesnt-install-anything) |

## Every `/api/*` call returns 404 (GLKVM)

On GL.iNet firmware the PiKVM REST API is **disabled by default**. Until you
enable it, every `/api/*` call returns 404 and `kvm-pilot` cannot talk to the
device. SSH into the unit (or use the app's terminal) and uncomment the
relevant block in:

```
/etc/kvmd/nginx-kvmd.conf
```

then restart the service (or reboot the unit). This is a GL firmware behavior,
not a `kvm-pilot` setting — stock PiKVM devices expose the API by default.

Two follow-ups worth knowing:

- **A firmware upgrade can revert this.** GL flashes rewrite
  `nginx-kvmd.conf`, so after any upgrade re-check the API and re-enable it if
  needed, then re-run `kvm-pilot healthcheck`
  (see [firmware-update.md](firmware-update.md)).
- **kvm-pilot detects the condition.** With the GL driver
  (`--driver glkvm`, `KVM_PILOT_DRIVER=glkvm`, or `driver = "glkvm"` in the
  profile), a 404 across `/api/*` surfaces as a clear, actionable
  `ApiDisabledError` pointing at `nginx-kvmd.conf` — and you can preflight
  with `check_api_enabled()`.

## `snapshot` fails or lies

![The snapshot pipeline: a snapshot call finds the on-demand GL encoder idle; kvm-pilot connects a WebSocket video client to wake it and a JPEG plus live signal state comes back. Failure exits: a 503 on a headless unit (encoder asleep, not a wedge), a tiny undecodable H.264 frame at native resolution, and a black frame with signal present (host display asleep).](snapshot-pipeline.svg)

- **HTTP 503 on a headless unit** — the GL encoder is **on-demand**: it only
  runs while a video client is connected, so "nobody watching" used to 503
  forever. kvm-pilot now wakes it automatically by connecting a WebSocket
  video client (~1.5 s) before snapshotting (#142). If a 503 persists, pull
  `logs` and run `healthcheck` — its `encoder-wedge` finding names a **true**
  wedge, which an appliance reboot clears (`appliance_reboot`, or SSH).
- **Tiny/undecodable frame while a signal is present** — H.264 at the panel's
  native resolution can't produce a JPEG still (#107/#151; surfaced as
  `SnapshotFormatError`, not a fake success). See the screen live via the
  WebRTC stream / vendor web UI, or drop the host's resolution.
- **Black frame while `powered_on` reads true** — on units without a wired
  ATX board the power reading is **not trusted** (`is_powered_on` fails
  open), and a black frame with `hdmi_signal: true` usually means the host
  display is asleep (DPMS). Send a harmless keystroke to wake it, and
  disambiguate with an SSH reachability check to the target host — visual
  checks are exactly what fails on a black screen.
- **Stale pixels** — a byte-identical frame across an expected screen change
  (`unchanged_since_last_snapshot: true`) means cached/stale video. Verify
  via the `signal` block and `logs` before acting on what you see.

## Act tools denied: `approval cancel` / `denied by approver`

**Symptom:** every act tool returns `approved: false` with `approver: null`
and `denied_reason: "approval cancel"` (or `"denied by approver"` /
`"approval decline"` after a mis-click), while read-only tools (`snapshot`,
`info`, `logs`) keep working — it looks like the host is ignoring input.

**Cause:** the chat client killed the approval prompt, not the device. Chat
clients tie a pending elicitation to the conversational turn, so sending a new
message cancels the in-flight approval. **The action never reached the
target.** The denial result names this in its `remediation` field.

**Fix:** answer the approval prompt before sending another message —
`approval cancel` is benign and retryable. If per-call approvals keep getting
killed by that client, the operator can set `KVM_PILOT_MCP_ELICIT=off` in the
server env and reconnect: the `ALLOW_*` effect gate plus per-call
`confirm=true` then become the standing authorization. **Trade-off:** that
disables per-call human approval — an operator decision, not a default.
Details: [MCP server README](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/mcp/README.md).

## The host went dark (no video, no HID, no ping)

No video + no HID + no ping, with a stale ARP entry, is almost always a
**suspended or powered-off host**, not a broken KVM. Recover remotely, in
this order (the bundled skill carries the full doctrine):

1. **Wake-on-LAN first** — cheap, low-risk, non-invasive: `kvm-pilot wake`
   (or the MCP `wake` tool) sends the magic packet; a suspended host wakes in
   seconds. Don't burn time on HID re-enumeration or appliance reboots before
   ruling WoL out.
2. **SSH to the target's own address** once it answers (`ssh_reachable` →
   `ssh_exec`) — faster and more reliable than typing through KVM HID.
3. **Only then** treat it as a KVM-side fault: `recover-hid`, then an
   appliance reboot for a wedged encoder.
4. Physical intervention is the last resort.

Prevention: disable idle-suspend on any host that must stay reachable —
GNOME/Fedora auto-suspends even at the login screen, dropping video, HID,
and network at once (`systemctl mask sleep.target suspend.target`).

## Mouse clicks land in the wrong place

Some capture chains scale or offset the pointer. Run `kvm-pilot
calibrate-mouse` (or the MCP `calibrate_mouse` tool): it measures and stores
this host's commanded→observed correction (5-point grid + held-out verify,
~10–30 s on a static screen), after which `mouse` percent coordinates apply
it transparently and report `calibrated: true`. The calibration is stored per
(host, capture resolution) — a resolution change makes it stale, and it is
then never applied. Needs Pillow (`pip install 'kvm-pilot[calibrate]'`).

## SOL serial console shows binary noise (iDRAC6)

On Dell iDRAC6 (e.g. PowerEdge R710), SOL is wired to **COM2**. If the BIOS
has Serial Communication set to redirect via COM1 (a common default),
`kvm-pilot console` shows only binary noise. Set **BIOS → Serial
Communication → "On with Console Redirection via COM2"**. For Linux serial
consoles that's `ttyS1` (`console=ttyS1,115200`). SOL is text-only: it drives
Linux/ESXi text installers and GRUB fine, but not a graphical installer.

## `pip install kvm-pilot` doesn't install anything

The current release line is a **pre-release**, and plain `pip install
kvm-pilot` deliberately picks up no pre-release. Use:

```bash
pip install --pre kvm-pilot
```

(`0.1.0a1` is yanked and ancient — don't pin it.)

## FAQ

- **Do I point kvm-pilot at the KVM or at the server it controls?** At the
  **KVM appliance's** address. The managed host is a separate machine with a
  separate IP — see the two-machines picture in
  [getting-started.md](getting-started.md); its SSH address goes in
  `ssh_host`.
- **Why does `power_state` say off while the host is clearly running?** No
  wired ATX adapter → the device can't sense power honestly, and the driver
  marks the reading untrusted rather than guessing. Wire the ATX board (GL
  Comet family: the separately sold GL-ATXPC) for real power sensing/control.
- **Why won't it flash firmware remotely on my GL-RM1PE?** The remote flash
  endpoint no-ops on that hardware (#94/#95) — kvm-pilot verifies the
  upgrade state and reports the failure instead of pretending. Use the GL web
  console (the only known-good path); see
  [firmware-update.md](firmware-update.md).
- **Is my hardware supported?** Check the
  [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
  — it's generated from real run evidence only. If your combo isn't there,
  `kvm-pilot test-report` contributes it in one command.
