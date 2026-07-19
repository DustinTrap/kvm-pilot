# Hardware test plan — HPE DL380 G9 (iLO) + Dell R710 (iDRAC)

A fast, copy-paste runbook to validate the Redfish driver — especially the new
**boot-device control** (BootSourceOverride, #28/#201) and **power** — against two
real BMCs. Tracks epic #200; capture findings on #29 (real-BMC validation) and the
support matrix (#96). Pairs with the synthetic coverage in
`tests/test_redfish_boot.py` / `tests/redfish_emulator.py`.

> **Status:** the **R710/iDRAC6 leg was executed 2026-07-14** — the IPMI driver
> (#62, shipped in v0.1.0b7) validated live end-to-end (power, boot-device,
> sensors, SEL, SOL console; results on #206 and the
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)).
> The **DL380 G9 / iLO4 Redfish leg (#29) is still open** — the runbook below
> remains the procedure for it, and a reusable template for similar boxes.

> **Read the firmware-era caveat first — it decides which box is testable over Redfish.**

## Firmware reality (important)

| Box | BMC | Redfish? | Path |
|---|---|---|---|
| **HPE DL380 G9** | **iLO 4** (fw ≥ 2.30 for DMTF Redfish 1.0; 2.5x+ preferred) | **Yes** (older Redfish — exactly what the driver's feature-detect + quirk handling targets) | `--driver redfish` ✅ |
| **Dell R710** | **iDRAC 6** (11th-gen; there is no iDRAC7 for R710) | **No** — iDRAC6 predates Redfish (WS-MAN/IPMI/racadm only) | Redfish will 404 at `/redfish/v1`; use the **IPMI driver** (`driver = "ipmi"`, #62 — shipped v0.1.0b7, live-validated on this exact box 2026-07-14) |

So: **full Redfish run on the DL380 G9**; on the R710, **confirm clean detection
of "no Redfish"**, then run the same phases through the IPMI driver (see the
R710 section below). If the R710's iDRAC was flashed to something newer, or
it's actually a 12th-gen board, re-check — but plan for iDRAC6 = no Redfish.

## Prerequisites

- kvm-pilot installed (`uv run kvm-pilot ...` in the repo, or `pip install`).
- Network reachability from the runner to each BMC IP (BMCs are on a dedicated
  mgmt NIC/VLAN — confirm the runner can reach it; the same split-tunnel/L2 gotcha
  that hid `10.0.1.16` applies).
- Credentials. **Do not commit them.** Defaults to try: iLO4 `Administrator` /
  the label password on the server pull-tab; iDRAC `root` / `calvin`.
- A maintenance window — several steps power-cycle the host.

## Onboarding (config profiles)

Add to `~/.config/kvm-pilot/config.toml` (password via `KVM_PILOT_PASSWD`,
`--passwd-file <600 file>`, or `--ask-passwd` — never in the file):

```toml
[hosts.ilo-dl380g9]
host = "10.0.1.AA"        # iLO IP
driver = "redfish"
user = "Administrator"
verify_ssl = false         # iLO4 ships a self-signed cert; pin with ssl_ca_file if you have it

[hosts.idrac-r710]
host = "10.0.1.BB"        # iDRAC IP
driver = "ipmi"            # iDRAC6 has no Redfish; shells out to ipmitool
user = "root"
```

Then per session: `export KVM_PILOT_PASSWD='...'` (or pass `--ask-passwd`).

---

## Phase A — read-only vetting (safe; run first)

```bash
P=ilo-dl380g9   # then repeat with P=idrac-r710
uv run kvm-pilot healthcheck   --profile $P     # intake gate — expect no CRITICAL for a reachable BMC
uv run kvm-pilot info          --profile $P     # manufacturer/model/BIOS/power_state/redfish_version
uv run kvm-pilot capabilities  --profile $P     # expect: system_info, power, boot_progress, sensors, logs, virtual_media, boot_config
uv run kvm-pilot power-state   --profile $P
uv run kvm-pilot boot-device   --profile $P --show   # current override + ALLOWABLE targets + mode_settable
uv run kvm-pilot sensors       --profile $P     # temps/fans/power (iLO4/iDRAC expose these)
uv run kvm-pilot boot-progress --profile $P
uv run kvm-pilot logs          --profile $P --seek 3600
```

**Record from `info` / `boot-device --show`:** `redfish_version`, the
`allowable` boot targets, and `mode_settable` (does the box expose
`BootSourceOverrideMode`?). These drive the quirks table below.

---

## Phase B — boot-device control (the new feature, #28/#201)

`--show` is read-only; every *set* needs `--yes` (gated) and writes
BootSourceOverride. Verify each with a follow-up `--show`.

```bash
P=ilo-dl380g9
# one-time PXE (default: once + UEFI)
uv run kvm-pilot boot-device pxe  --profile $P --yes
uv run kvm-pilot boot-device      --profile $P --show      # expect enabled=Once, target=pxe

# one-time CD/virtual-media, then HDD
uv run kvm-pilot boot-device cd   --profile $P --yes
uv run kvm-pilot boot-device hdd  --profile $P --yes

# persistent + legacy variants (exercise both flags)
uv run kvm-pilot boot-device pxe  --profile $P --persistent --yes   # enabled=Continuous
uv run kvm-pilot boot-device bios --profile $P --legacy --yes       # BootSourceOverrideMode=Legacy (if settable)

# clear the override
uv run kvm-pilot boot-device none --profile $P --yes                # enabled=Disabled
```

**End-to-end confirmation (optional, needs a reboot):** set `pxe` once, then
`power reset --profile $P --yes`, and confirm on the console/iLO that it PXE-boots
once and reverts afterward.

**Watch for (and note on #29):**
- A target in your list that the BMC rejects → the driver should raise a clear
  `CapabilityError` naming the allowable set (not an opaque 400).
- iLO4 rejecting `BootSourceOverrideMode` → the driver **auto-retries without it**
  (log line: "rejected BootSourceOverrideMode; retrying without it"); confirm the
  target still applied.
- A `202 Accepted` + Task on the PATCH → the driver polls it to completion.

---

## Phase C — power (destructive; maintenance window)

```bash
P=ilo-dl380g9
uv run kvm-pilot power-state  --profile $P
uv run kvm-pilot power on     --profile $P --yes    # verified against Redfish PowerState
uv run kvm-pilot power off    --profile $P --yes    # graceful (GracefulShutdown)
uv run kvm-pilot power reset  --profile $P --yes
uv run kvm-pilot power-cycle  --profile $P --yes    # off-hard -> on, blocks on PowerState
```
Redfish power is **verified** (real PowerState), unlike GL ATX. Note the
`ResetType`s the box advertises (iLO4 vs iDRAC differ) and any
`InvalidOperationForSystemState` 400/409 the driver absorbs as success.

## Phase D — virtual media (if the BMC exposes it over Redfish)

```bash
uv run kvm-pilot media-list --profile ilo-dl380g9
# uv run kvm-pilot mount <http-url-to.iso> --profile ilo-dl380g9 --yes
# uv run kvm-pilot eject --profile ilo-dl380g9 --yes
```
iLO4 needs an advanced/iLO license for scriptable virtual media — note if it's
absent. Combine with `boot-device cd` for a full remote-install rehearsal.

## Phase E — Wake-on-LAN (wired NIC; alternative power-on, #199)

For the host's OS NIC (not the BMC): confirm `ethtool <if>` shows `Wake-on: g`,
note the wired MAC, suspend/shutdown the OS, then from a sender on the same L2:
```bash
uv run python -c "from kvm_pilot import wol; wol.send_magic_packet('AA:BB:CC:DD:EE:FF', broadcast='10.0.1.255')"
```
(Already hardware-validated on the RHEL host behind KVM .20 — woke in 52 s.)

---

## R710 / iDRAC6 — the IPMI path (executed 2026-07-14)

iDRAC6 has no Redfish (a probe at `/redfish/v1` fails with 404 / connection
reset) — the box runs through the **IPMI driver** (#62, `driver = "ipmi"`,
shells out to the system `ipmitool`). The same phase structure applies:

```bash
P=idrac-r710
uv run kvm-pilot healthcheck   --profile $P     # intake gate
uv run kvm-pilot info          --profile $P     # model from FRU Board Product (#206)
uv run kvm-pilot power-state   --profile $P
uv run kvm-pilot sensors       --profile $P     # temps/fans/PSU via SDR
uv run kvm-pilot logs          --profile $P     # SEL
uv run kvm-pilot boot-device pxe --profile $P --yes   # then cd/hdd/none, as Phase B
uv run kvm-pilot power reset   --profile $P --yes     # as Phase C
uv run kvm-pilot console       --profile $P            # SOL; exit with ~. (#208)
```

All of the above were **live-validated on the R710 (14 pass / 0 fail) on
2026-07-14**, including a SOL boot capture and an F11 boot-menu drive. Quirks
found: iDRAC6 maps SOL to **COM2** (`ttyS1` on the host), and the console is
text-only — no graphical/GUI installer phases over SOL.

---

## Quirks to capture (→ #29 / support matrix #96)

For each box, record:
- [ ] `RedfishVersion` and BMC firmware version
- [ ] `capabilities()` set actually served
- [ ] boot-device `allowable` targets + whether `mode_settable`
- [ ] Did `BootSourceOverrideMode` apply, get rejected (auto-retry), or absent?
- [ ] one-time vs persistent both honored? cleared cleanly?
- [ ] PATCH sync (200/204) vs async (202+Task)?
- [ ] Power `ResetType`s advertised; any 400/409-absorbed-as-success
- [ ] Virtual media present/licensed?
- [ ] Auth: session vs basic; session-cap/idle-timeout behavior
- [ ] Any field/shape that differs from the emulator (feed it back into `redfish_emulator.py`)

## Results table (fill in)

| Check | DL380 G9 (iLO4) | R710 (iDRAC6, via IPMI — 2026-07-14) |
|---|---|---|
| Reachable / healthcheck | | ✅ |
| info + redfish_version | | ✅ (no Redfish; model via FRU) |
| capabilities | | ✅ (IPMI set) |
| boot-device --show (allowable, mode) | | ✅ |
| set once: pxe / cd / hdd | | ✅ |
| set persistent / legacy | | ✅ |
| clear (none) | | ✅ |
| power on/off/reset/cycle | | ✅ |
| virtual media | | n/a (not exposed via IPMI) |
| WoL (host NIC) | | |
| SOL console | n/a (Redfish: descriptor only) | ✅ (COM2/`ttyS1`, text-only) |
| Notes / quirks | | 14/0; details on #206 + the HCL page |
