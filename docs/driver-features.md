# Per-driver feature list: capability, reliability & testing level

> Tracking issue: [#171](https://github.com/DustinTrap/kvm-pilot/issues/171).
> Related: [#96](https://github.com/DustinTrap/kvm-pilot/issues/96) (support-matrix
> epic), [#102](https://github.com/DustinTrap/kvm-pilot/issues/102) /
> [#103](https://github.com/DustinTrap/kvm-pilot/issues/103) (the shipped run
> ledger + its docs), and [#152](https://github.com/DustinTrap/kvm-pilot/issues/152).

This page lists, **for every driver**, the complete set of capabilities it can
expose and — per capability — how reliably it behaves on real hardware and how
far it has actually been tested. It exists so an operator or agent can answer
"can this driver do X, and can I trust the result?" without reading the source.

> **Early alpha — read this before you trust a rating.** *Structural* support
> (the driver implements a protocol) is **not** the same as *verified* support.
> Most device+capability combinations here are mock- or emulator-only. The
> **single source of truth** for what has actually been exercised on real
> hardware is the run ledger (`src/kvm_pilot/data/test_runs.jsonl`, surfaced by
> `kvm-pilot capabilities` and the MCP `support_matrix` tool) and the community
> [Hardware-Compatibility](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> wiki page. Where this page and the ledger disagree, **the ledger wins** — the
> tables below are refreshed by hand and can lag.

## Three axes, kept separate

A feature is described along three independent axes. Conflating them is exactly
the mistake that leads to over-claiming maturity.

1. **Structural capability** — does the driver implement the capability protocol
   in [`drivers/base.py`](../src/kvm_pilot/drivers/base.py)? Detected
   structurally (`isinstance` against a `@runtime_checkable` Protocol), never
   hand-declared. `kvm-pilot capabilities --driver <kind>` prints this set
   offline.
2. **Reliability** — when it *is* implemented, does it work **consistently** on
   real hardware, and — critically — does it ever **report success while lying**?
   Several devices return a value that looks fine but isn't (ATX power says
   "off" on a running host; an old GL snapshot returns H.264 bytes mislabeled
   as JPEG; a firmware flash returns 200 and no-ops). Reliability captures those
   false-report modes, which are more dangerous than an honest error.
3. **Testing level** — how far has the code path actually been exercised? Unit
   test with the transport mocked? Against a stdlib emulator in CI? Or on a real
   device with an entry in the run ledger?

## Legends

### Reliability

| Rating | Meaning |
|---|---|
| **reliable** | Verified to work consistently on real hardware, with no known false-report mode. |
| **conditional** | Works, but only under documented conditions or with a known caveat (a firmware floor, a resolution ceiling, the REST API must be enabled, the display must be awake). |
| **false-report** | The call can *appear* to succeed while the result is untrustworthy — you must confirm out-of-band. The most dangerous rating; each one is spelled out in Notes. |
| **unverified** | Structurally implemented but never confirmed on real hardware (mock/emulator only). No claim either way. |
| **n/a** | Not implemented by this driver. |

### Testing level

| Level | Meaning |
|---|---|
| **mock** | Unit tests only, transport mocked (`tests/conftest.py`). Exercises the code, touches no device. |
| **emulator** | Exercised over the **real transport** against a pure-stdlib fake server in CI: `tests/emulator.py` (kvmd) for the PiKVM family, `tests/redfish_emulator.py` and the external DMTF **sushy-tools** for Redfish. Proves the path is wired correctly; proves nothing about a physical device. |
| **live:`<vendor> <product>@<fw>`** | Confirmed on real hardware, with a row in `test_runs.jsonl`. Annotated with the **derived maturity** (below) where the ledger provides one, e.g. `live:gl.inet RM1PE@V1.9.1 (beta)`. |
| **n/a** | Not applicable (capability not implemented). |

### Derived maturity (issue #98)

On top of live evidence, `kvm_pilot.maturity` promotes each
`(vendor, product, firmware)` × capability along a ladder — **derived from the
ledger, never hand-set** (CI fails on drift):

| Level | Rule (per capability, from its live pass history) |
|---|---|
| `alpha` | 0 live passes (mocks only, or live failures only) |
| `beta` | ≥ 1 live pass |
| `rc` | ≥ 3 live passes across ≥ 2 distinct UTC dates |
| `ga` | ≥ 5 live passes spanning ≥ 14 days, all after the most recent live failure |

See [firmware-registry.md](firmware-registry.md#maturity-derived-from-the-run-ledger-98).

### The capability vocabulary

Every driver is scored against the full `Capability` enum, so "not supported"
is stated explicitly rather than left blank.

| Capability | Protocol | What it is |
|---|---|---|
| `system_info` | `SystemInfo` | Read device/host identity + state. |
| `power` | `Power` | Read and change host power state. |
| `hid` | `HID` | Emulated keyboard + mouse. |
| `video` | `Video` | Still-frame capture (feeds the vision layer). |
| `virtual_media` | `VirtualMedia` | Attach/detach an ISO or USB image. |
| `gpio` | `GPIO` | Drive relays / power buttons / LEDs. |
| `events` | `Events` | Stream async device events. |
| `logs` | `Logs` | Device/host event log (`seek` = seconds of lookback). |
| `boot_progress` | `BootProgress` | Host POST/boot phase as a structured token (no screenshot). |
| `sensors` | `Sensors` | Structured temps / fans / voltages / watts. |
| `serial_console` | `SerialConsole` | Read/write the host serial console as text (SOL). |
| `watchdog` | `Watchdog` | Arm/pet/inspect a hardware watchdog (IPMI). |
| `firmware_update` | `FirmwareUpdate` | Flash the **KVM/BMC's own** firmware over the network. |
| `ssh` | `RemoteShell` | In-band control of the managed **host's** OS over SSH. **Not a driver capability** — a per-profile channel (see note in each table). |

---

## `glkvm` — GL.iNet fork (GL-RM1 "Comet" / GL-RM1PE "Comet PE")

The **primary live-tested target.** GLKVM speaks the kvmd REST API
(`PiKVMDriver` is the base) but diverges enough to own its module
([`drivers/glkvm.py`](../src/kvm_pilot/drivers/glkvm.py), #140). Two GL-RM1PE
firmware lines have real ledger runs: **V1.5.1 release2** (kvmd 4.82) and
**V1.9.1 release1**.

**Driver-wide preconditions and quirks** (from `GLKVM_QUIRKS`; `source` shown):

- **REST API disabled by default** (`api-disabled-by-default`, *documented*):
  every `/api/*` 404s until enabled in `/etc/kvmd/nginx-kvmd.conf` and kvmd is
  restarted — and a firmware update can silently revert it. The driver turns the
  bare 404 into an actionable `ApiDisabledError`. This gates **all** capabilities
  below, so each is at best `conditional` on the API being enabled.
- **Masquerades as a Raspberry Pi PiKVM.** `/api/info` self-reports
  `type: rpi, board: rpi4` even on Rockchip RV1126 hardware; `get_firmware_info`
  reads the real product version from `/api/upgrade/version` instead.
- **ATX sensing is not wired** (`atx-power-state-always-off`, *observed* on kvmd
  4.82): `/api/atx` reports `power=off`, `enabled=false`, LEDs false even on a
  fully booted host. Registry `profile.power_state_trusted = false`.

Structural set: `system_info, power, hid, video, virtual_media, gpio, events, logs, firmware_update`.

| Capability | CLI / MCP surface | Reliability | Testing level | Notes / quirks |
|---|---|---|---|---|
| `system_info` | `info` · MCP `info` | conditional | live:gl.inet RM1PE@V1.5.1 (beta), @V1.9.1 (beta) | Fields present and correct, but the raw identity **lies** (rpi/rpi4 on RV1126); trust `get_firmware_info` (reads `/api/upgrade/version`). Dual version numbers (GL product vs kvmd component). |
| `power` | `power`, `power-cycle` · MCP `power`, `power_state` | **false-report** | read: live (untrusted); actuate: unverified | ATX **always reports off / enabled=false / LEDs false** on RM1PE even when running — `is_powered_on()` and the *result* of on/off/cycle **cannot be confirmed via ATX**. Verify visually (snapshot/vision). Power *actuation* has **no ledger run** (`never_exercised: power`). |
| `hid` | `type`, `key`, `mouse-move`, `click` · MCP `type_text`, `press_key`, `send_shortcut`, `ctrl_alt_delete`, `mouse` | conditional | emulator; live-observed (not yet in ledger) | Works, but the emulated USB HID gadget can drop to **busy/unplugged** on real RM1PE units (#155), blanking keyboard/mouse until re-enumerated. Mitigations shipped: `recover_hid` (#160), keep-awake jiggler (#159), `display_awake` (#161). Not captured as a formal ledger capability yet — a **sweep gap**. |
| `video` | `snapshot`, `classify`, `watch` · MCP `snapshot`, `classify_screen`, `wait_for_state` | conditional | live:gl.inet RM1PE@V1.9.1 (beta, 6/6 pass); @V1.5.1 mixed (1 pass / 1 fail) | Reliable on **V1.9.1** (cached JPEG, decoupled from the encoder). On **V1.5.1** the JPEG endpoint returns **H.264 bytes mislabeled `image/jpeg`** at >1080p (#107) — formerly a silent false-report, **now surfaced** as `SnapshotFormatError`. Separate **503** modes: display asleep/DPMS (`hdmi.signal=false`, #126/#142) or idle on-demand streamer; the driver attaches the live streamer state to explain which. |
| `virtual_media` | `media-list`, `mount`, `eject` · MCP `list_virtual_media`, `mount_iso`, `eject` | **false-report** | emulator; live-observed insert (#78), not in ledger | GL accepts the mount calls and reports `connected=true` while `online` stays **false** and the host sees nothing when the MSD toggle is off (#77). Mitigation: `mount_iso` polls `/api/msd` for `online` and raises `MediaOfflineError`. Positive tell observed live: host boot menu showed "Glinet Optical Drive" when truly presented (#78). `never_exercised: virtual_media` in the ledger. |
| `gpio` | library API (`gpio_switch`/`gpio_pulse`); no CLI/MCP | unverified | emulator/mock | Inherited from the PiKVM base, but **RM1PE exposes no GPIO channels** (`ATX enabled=false, no GPIO`). Present in the API surface, effectively unusable on this hardware. |
| `events` | `events` (needs `ws` extra) | unverified | mock | WebSocket event stream; no real-hardware run. |
| `logs` | `logs` · MCP `logs` | **reliable** | live:gl.inet RM1PE@V1.5.1 (beta), @V1.9.1 (beta) | kvmd `/api/log`; `seek` = seconds of lookback. Verified on both firmwares — it's how the RV1126 encoder wedge (venc/vpss/vvi in D-state) was diagnosed. `follow`/tail is refused (blocking transport). |
| `boot_progress` | `boot-progress` | n/a | n/a | Not implemented by the PiKVM family — boot phase comes from **vision** (`classify`), not a structured enum. |
| `sensors` | `sensors` | n/a | n/a | `Sensors` protocol not implemented. Raw Prometheus metrics exist (`get_metrics`, `/api/export`) but are not the structured capability. |
| `serial_console` | — | n/a | n/a | Not implemented. |
| `watchdog` | — | n/a | n/a | Not implemented. |
| `firmware_update` | `firmware-update`, `firmware-check` | **false-report** | live:gl.inet RM1PE@V1.5.1 (alpha, **FAILED**) | GL `/api/upgrade/*` (provisional, reverse-engineered). On RM1PE the `start` POST **returns 200 and no-ops** (#94/#95) — the driver now verifies an actual upgrade-state change and reports failure otherwise. Registry `remote_update`: `risk=high, recovery_required=true, self_flash_blind=true` (physical U-Boot recovery only; a flash can re-disable the REST API and can corrupt if media is mounted). See [firmware-update.md](firmware-update.md). |
| `ssh` | `ssh-check`, `ssh-exec` · MCP `ssh_reachable`, `ssh_exec` | (per-profile) | n/a as a driver cap | Not a GLKVM capability. In-band SSH to the **managed host's** OS is a per-profile channel (`ssh_*` config, #81). Separately, an **appliance-SSH** channel to the KVM's *own* OS (#162) backs `kvm-pilot appliance-*` / `paths` for encoder-wedge recovery REST can't see. |

## `pikvm` — stock PiKVM (canonical base)

`PiKVMDriver` in [`client.py`](../src/kvm_pilot/client.py) — the full kvmd REST
client and the base of the whole family. **No stock-PiKVM device has a run in
the ledger**; the GL fork is what has actually been on hardware. Everything
below is therefore `unverified` on real hardware, though the read/HID/power/MSD
paths are exercised over the real transport against the kvmd emulator in CI.

Structural set: `system_info, power, hid, video, virtual_media, gpio, events, logs`.

| Capability | CLI / MCP surface | Reliability | Testing level | Notes |
|---|---|---|---|---|
| `system_info` | `info` · MCP `info` | unverified | emulator | kvmd `/api/info`. |
| `power` | `power`, `power-cycle` · MCP `power`, `power_state` | unverified | emulator | ATX. `is_powered_on` fail-opens when no ATX board is wired (returns "on" rather than a false "off"); on real stock PiKVM with an ATX board it should be trustworthy, but that is **unverified**. |
| `hid` | `type`, `key`, `mouse-move`, `click` · MCP HID tools | unverified | emulator | Keyboard/mouse over kvmd. |
| `video` | `snapshot`, `classify`, `watch` · MCP `snapshot`, `classify_screen`, `wait_for_state` | unverified | emulator | MJPEG snapshot with JPEG-header validation (#107). |
| `virtual_media` | `media-list`, `mount`, `eject` · MCP MSD tools | unverified | emulator | Upload/select/attach with online-verify. |
| `gpio` | library API; no CLI/MCP | unverified | emulator/mock | `/api/gpio`. |
| `events` | `events` (needs `ws` extra) | unverified | mock | WebSocket. |
| `logs` | `logs` · MCP `logs` | unverified | emulator | kvmd `/api/log`; `follow` refused. |
| `boot_progress` | `boot-progress` | n/a | n/a | Not implemented (vision instead). |
| `sensors` | `sensors` | n/a | n/a | Not implemented (Prometheus metrics only). |
| `serial_console` | — | n/a | n/a | Not implemented. |
| `watchdog` | — | n/a | n/a | Not implemented. |
| `firmware_update` | `firmware-update` | n/a | n/a | **Not implemented** — stock PiKVM has no OS-update REST API (it updates via `pikvm-update` over SSH). |
| `ssh` | `ssh-check`, `ssh-exec` | (per-profile) | n/a as a driver cap | Per-profile in-band channel, as above. |

## `blikvm` — BliKVM

[`BliKVMDriver`](../src/kvm_pilot/drivers/pikvm.py) is a **thin subclass of the
PiKVM base with no known deltas yet** — it exists so BliKVM-specific behavior has
a home. Its capability set, reliability, and testing level are **identical to
`pikvm`** above: structural set `system_info, power, hid, video, virtual_media,
gpio, events, logs`; everything `unverified` on real hardware (no BliKVM ledger
run), read/HID/power/MSD paths `emulator`-tested; `boot_progress`, `sensors`,
`serial_console`, `watchdog`, `firmware_update` not implemented. Any BliKVM
quirk discovered on hardware should be added to that subclass and reflected
here.

## `redfish` — DMTF Redfish BMC (iDRAC / iLO / Supermicro / Lenovo XCC / OpenBMC)

[`RedfishDriver`](../src/kvm_pilot/drivers/redfish/driver.py) — one stdlib client
for every DMTF-conformant BMC, portable by **navigating hypermedia** (follows
`@odata.id`, reads `@Redfish.ActionInfo`/`AllowableValues`; no hard-coded vendor
ids). Its capability set is **complementary** to a PiKVM's: strong on structured
state, **no pixels, no keyboard/mouse**. Per its own docstring it is **"alpha,
mock-tested only — never run against real hardware"**, but the whole set is
validated end-to-end against the external **sushy-tools** emulator in CI, so its
testing level is `emulator`, not bare `mock`. No live ledger rows exist yet, so
every reliability rating is `unverified`.

Structural set: `system_info, power, virtual_media, logs, boot_progress, sensors`.

| Capability | CLI / MCP surface | Reliability | Testing level | Notes |
|---|---|---|---|---|
| `system_info` | `info` · MCP `info` | unverified | emulator (sushy-tools) | ComputerSystem identity + volatile power/health/boot re-read fresh. |
| `power` | `power`, `power-cycle` · MCP `power`, `power_state` | unverified | emulator (sushy-tools) | `ComputerSystem.Reset`, ResetType chosen by intersecting a preference list with the target's advertised `AllowableValues`; blocks on the real `PowerState` transition. In principle **more trustworthy than ATX**, but unverified on hardware. |
| `virtual_media` | `mount`, `eject` · MCP `mount_iso`, `eject` | unverified | emulator (sushy-tools) | `InsertMedia`/`EjectMedia`; adapts to strict BMCs (drops optional body fields, retries with `TransferProtocolType`). No separate connect step. |
| `logs` | `logs` · MCP `logs` | unverified | emulator (sushy-tools) | SEL / lifecycle / IML via LogServices; `seek` = seconds, filtered on `LogEntry.Created`. `follow` refused. |
| `boot_progress` | `boot-progress` | unverified | emulator (sushy-tools) | **The Redfish standout** — structured `BootProgress.LastState` mapped to the project's phase vocabulary; no screenshot needed. |
| `sensors` | `sensors` · CLI `sensors` | unverified | emulator (sushy-tools) | Unified `Sensors` collection with `Thermal`/`Power` fallback; `$expand` fan-out where advertised. |
| `hid` | — | n/a | n/a | A BMC has no keyboard/mouse. Deliberately not implemented. |
| `video` | — | n/a | n/a | No screenshot endpoint. Vision is unavailable on Redfish. |
| `gpio` | — | n/a | n/a | Not applicable. |
| `events` | — | n/a | n/a | Redfish EventService (push/SSE) not implemented in this version. |
| `serial_console` | — | n/a | n/a | Redfish exposes SOL as an SSH/IPMI descriptor, not an HTTP byte stream — not implemented. |
| `watchdog` | — | n/a | n/a | An IPMI primitive; not implemented. |
| `firmware_update` | — | n/a | n/a | Redfish `UpdateService` not yet implemented (a later step). |
| `ssh` | `ssh-check`, `ssh-exec` | (per-profile) | n/a as a driver cap | Per-profile in-band channel, as above. |

## `fake` — in-process test double (no hardware)

[`FakeDriver`](../src/kvm_pilot/drivers/fake.py) implements the capability
protocols over mutable in-memory state: no network, no hardware. It is the
device double for tests, demos, and CI, and the **reference implementer of
`BootProgress`**. Destructive ops still route through the same `SafetyPolicy`
op-ids as a real driver, so it is a faithful stand-in for the safety layer, the
analyzer, and the CLI.

Reliability here means only "does the double behave deterministically" — it
**proves nothing about any physical device.** Testing level is `mock` by
definition (it *is* the mock). Never point it at, or infer anything about, real
hardware.

Structural set: `system_info, power, hid, video, virtual_media, gpio, events, logs, boot_progress`.

| Capability | Reliability | Testing level | Notes |
|---|---|---|---|
| `system_info` | deterministic (test double) | mock | Canned `{host, driver, powered, phase}`. |
| `power` | deterministic (test double) | mock | Mutates in-memory `powered`; gated identically to a real driver. |
| `hid` | deterministic (test double) | mock | Records keystrokes/clicks; scriptable HID-online state (#160/#161 stand-ins). |
| `video` | deterministic (test double) | mock | Returns a stub JPEG; scriptable OCR text and video-signal flag. |
| `virtual_media` | deterministic (test double) | mock | In-memory mount history; kvmd-shaped MSD state. |
| `gpio` | deterministic (test double) | mock | Records switch/pulse. |
| `events` | deterministic (test double) | mock | Replays a queued event list, then stops (unlike the real unbounded stream). |
| `logs` | deterministic (test double) | mock | Returns canned log text. |
| `boot_progress` | deterministic (test double) | mock | First `BootProgress` implementer; returns the scripted phase (or `None` while powered off). |
| `sensors` / `serial_console` / `watchdog` / `firmware_update` / `ssh` | n/a | n/a | Not implemented. |

---

## Live-fleet sweep evidence (in progress)

> **Status: sweep underway — this section is a hook, not yet complete.**

A live reliability sweep of the GL fleet — **`.11`** (`homelab`, GL-RM1PE),
**`.20`** (`homelab2`), and **`.39`** — is in progress to move `glkvm` ratings
above from "unverified / observed-but-not-in-ledger" to ledger-backed evidence,
especially for the capabilities with **no current ledger run**: `hid`,
`virtual_media`, `power` **actuation**, `gpio`, and `events`. It follows the
reusable [Hardware reliability test plan](test-plan.md)
([#172](https://github.com/DustinTrap/kvm-pilot/issues/172)) — every function,
in multiple device states, cross-checked against independent ground truth to
catch false reports. As runs are captured they land in
`src/kvm_pilot/data/test_runs.jsonl`, `kvm_pilot.maturity` re-derives the
levels, and the `glkvm` table's Reliability / Testing-level columns are
refreshed to match.

Until then, the only ledger-backed `glkvm` evidence is the 2026-07-03 GL-RM1PE
runs (V1.5.1 release2 and V1.9.1 release1) reflected above. Confirm the current
truth any time with:

```bash
kvm-pilot capabilities --driver glkvm --host <ip>     # structural set (offline)
# live evidence + derived maturity:
python -c "from kvm_pilot.support_matrix import rollup; import json; print(json.dumps(rollup(), indent=2, default=str))"
```

| Unit | Model / target | Firmware | Sweep status | Capabilities newly exercised |
|---|---|---|---|---|
| `.11` | GL-RM1PE (`homelab`) | _tbd_ | pending | _to be filled from the sweep_ |
| `.20` | `homelab2` | _tbd_ | pending | _to be filled from the sweep_ |
| `.39` | _tbd_ | _tbd_ | pending | _to be filled from the sweep_ |

## See also

- [Hardware-Compatibility (wiki)](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility) — the community source of truth for real-hardware runs.
- [Hardware reliability test plan](test-plan.md) — the reusable fleet-sweep procedure that produces the ledger evidence this page rates from ([#172](https://github.com/DustinTrap/kvm-pilot/issues/172)).
- [Architecture](architecture.md) — the capability-protocol design and the cross-device capability matrix.
- [Firmware registry](firmware-registry.md) — currency, capability profiles, and the derived-maturity ladder.
- [Remote firmware update](firmware-update.md) — the GL `/api/upgrade/*` surface and its high-risk reliability model.
- [CLI reference](cli.md) — every subcommand and the capability it needs.
