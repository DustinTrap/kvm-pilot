# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-14 · `main @ 1f93285` · **v0.1.0b7 releasing to PyPI** (IPMI driver + SOL + ipmi_sim cross-check); R710 iDRAC6 validated live end-to-end; `ipmisim` test VM on the LAN via DHCP (`10.0.1.152`)

## Current state
**v0.1.0b7 is the IPMI beta** (release commit `1f93285` pushed; GitHub Release →
Trusted-Publishing → PyPI — confirm the run went green). It builds on the
boot-control + WoL epic (#200) that shipped as v0.1.0b6.

- **v0.1.0b7 (releasing 2026-07-14) — the IPMI line:**
  - **IPMI driver (#62/#206)** — `--driver ipmi` via `ipmitool -I lanplus` (pw via
    `IPMI_PASSWORD` env). Power / SystemInfo / BootConfig / Sensors / Logs.
  - **IPMI SOL serial console (#208)** — `SerialConsole` (`serial_read`/`serial_write`
    over a PTY-backed `sol activate`) + `kvm-pilot console` interactive command
    (exit `~.`). Gated `ipmi.serial_console` (HID_INPUT). Text-only.
  - **ipmi_sim cross-check (#207)** — `tests/integration/test_ipmi_external.py` vs an
    independent OpenIPMI BMC (6/6).
  - **Two real-hardware fixes (#62)** — `get_info` model from `Board Product` (Dell
    iDRAC6 puts `localhost` in `Product Name`); healthcheck now labels `ipmi@` not
    `pikvm@`.
  - **LIVE-VALIDATED on a real Dell iDRAC6 (R710), creds root/moody @ 10.0.1.169:**
    power off/on/reset (verified flips), boot round-trips, 105 sensors, SEL, info
    (14/0); SOL captured the full BIOS boot; **F11 over SOL opened the BIOS Boot
    Manager** (bidirectional drive proven). Redfish confirmed absent (as expected).

### Older: v0.1.0b6 (released 2026-07-14, PRs #203/#204/#205)
The **remote boot-control + Wake-on-LAN epic (#200)**:

- **v0.1.0b6 (released 2026-07-14, PRs #203/#204/#205):**
  - **Wake-on-LAN (#199)** — `wol.py` magic-packet core (hardware-validated: woke a
    suspended host in ~52s), `kvm-pilot wake` CLI + `wake` MCP tool + `mac`/
    `wol_broadcast` config (#23), and **`power on` falls back to WoL** when the KVM
    has no wired ATX/GPIO power path.
  - **Redfish boot-device control (#28/#201)** — `BOOT_CONFIG` capability +
    `BootConfig` protocol; `kvm-pilot boot-device <pxe|cd|hdd|usb|bios|diag|none>
    [--once|--persistent] [--legacy] [--show]` + `boot_options`/`set_boot_device`
    MCP tools; one-time/persistent, UEFI/legacy, feature-detect allowable + auto-
    retry-without-mode. Cross-checked against **sushy-tools** (found: `--fake` pins
    `BootSourceOverrideEnabled=Continuous`).
  - **In-band boot control (#150)** — `boot-device --via ssh` sets one-time UEFI
    `BootNext` via `efibootmgr` over the SSH channel (gated `ssh.set_boot_next`).

## Next steps
- **Verify the v0.1.0b7 release landed on PyPI** (`pip index versions kvm-pilot` /
  the project page) once the GitHub Release + release.yml finish.
- **DL380 G9 = iLO4 (#29) — the remaining hardware target.** HAS Redfish (quirky) →
  `--driver redfish`; full boot-device + power test. Runbook:
  [`docs/hardware-test-plan-ilo-idrac.md`](docs/hardware-test-plan-ilo-idrac.md).
  Needs the iLO IP + creds from the operator. The R710/iDRAC6 IPMI path is DONE.
- **Record Hardware-Compatibility evidence** for the iDRAC6/R710 (support-matrix
  honesty rule) — power/boot/sensors/logs/info + SOL all exercised live.
- **Deferred IPMI**: `Watchdog` (#13/#28); Redfish SOL (`SerialConsole` for
  RedfishDriver, #28). Optional: a clean SOL screen-renderer (VT100) for capture.
- Other open: OCR #202, router epic #181 remainder, #157 (EDID), #148 (.mcpb),
  #163 (ProxyJump), Reflexes #123–#117 (post-GA).
- **Next beta release**: bump `__about__.py`, move #206/#207 into a dated CHANGELOG
  section, `gh release create` (Trusted-Publishing → PyPI). Human call.
- **Doc debt (found by this checkpoint)**: `boot-device` and `wake` shipped in b6
  but are missing from `docs/cli.md` + `src/kvm_pilot/skill/SKILL.md` command lists.
- **Deferred IPMI**: SOL `SerialConsole` + `Watchdog` (#13/#28).
- Other open: OCR #202, router epic #181 remainder, #157 (EDID), #148 (.mcpb),
  #163 (ProxyJump), Reflexes #123–#117 (post-GA).

## Device state left non-default (this tool mutates real hardware)
- **`ipmisim` VM (ns `ipmi-test`) on the SNO cluster `sno-lab` — LEFT RUNNING per
  operator request, now on the LAN via DHCP.** Fedora + `OpenIPMI-lanserv` +
  `ipmitool` + `kvm-pilot@main`; repo clone with the integration test at
  `/home/fedora/kp`. **Reach it directly from the LAN:
  `ssh -i ~/.ssh/id_ed25519 fedora@10.0.1.152`** (DHCP lease; MAC of the LAN NIC
  `02:66:04:5f:f3:60` — reserve it on the UDM if a stable IP is wanted, else the
  lease may change on renewal/restart). ipmi_sim:
  `sudo ipmi_sim -c /etc/ipmi/lan.conf -f /etc/ipmi/ipmisim1.emu -n` (binds
  `localhost:9001`, `ipmiusr`/`test` — to hit it over the LAN, change lan.conf
  `addr` to `0.0.0.0`/the LAN IP + open the guest firewall).
- **Dell R710 (iDRAC6 @ `10.0.1.169`, root/moody) — TESTED, left ON.** Full IPMI
  suite run against it (power off→on→reset, boot round-trips, sensors, SEL, info +
  SOL + F11). BIOS **Serial Communication set to "Console Redirection via COM2"**
  (operator changed it — that's what made SOL show text; COM2 = `ttyS1` for the OS).
  No OS/boot media installed → it sits at the BIOS "No boot device available" prompt
  (Legacy boot mode). Boot device left at `none`. Reach IPMI from the LAN VM:
  `IPMI_PASSWORD=moody ipmitool -I lanplus -H 10.0.1.169 -U root -E …` or
  `kvm-pilot … --driver ipmi --host 10.0.1.169 --user root`. (The VM's `/home/fedora/kp`
  clone has b7 `ipmi.py`/`health.py` overlaid; `pip install -U --pre kvm-pilot` there
  once b7 is on PyPI to get the released code + the `console` command.)
- **Cluster networking changed to give the VM its LAN IP (additive, reversible):**
  installed the **Kubernetes NMState Operator** (`openshift-nmstate`), added NNCP
  **`ovn-lan-bridge-mapping`** (OVN `bridge-mappings: lan→br-ex`, `Available`), and
  a localnet NAD **`ipmi-test/lan`**. The VM has a **secondary `bridge` NIC** on
  that NAD (pod network kept primary). Node's primary/API path via br-ex was
  verified reachable throughout. To revert: remove the VM's `lan` interface/network,
  then `kubectl delete nncp ovn-lan-bridge-mapping net-attach-def/lan -n ipmi-test`.
- **`.16` = the KVM-`.20`-connected host** (RHEL 10.2, my `~/.ssh/id_ed25519`
  installed, passwordless): ethernet cable plugged in this session → **wired
  `10.0.1.16` up**; WoL validated (eno1 `5c:60:ba:bb:cf:63`). WiFi alias `.165`
  exists but the Mac can't reach it (client isolation) — use wired `.16`.
- **Physical KVM fleet `.11`/`.20`/`.39`** not touched post-compaction this session;
  prior state may persist (keep-awake/jiggler left ON in earlier sessions; `.39` =
  firmware V1.9.1). See [[sno-openshift-fed-ws-01-buildout]] for the cluster this
  IPMI VM runs on.

## Fleet facts (connected systems)
- **KVM `.11` → `fed-ws-01`** (Dell T7610) — now running **SNO OpenShift**
  (`sno-lab`, node 10.0.1.100), virt-ready (LVMS + CNV). The `ipmisim` VM runs here.
- **KVM `.20` → RHEL 10.2 host** (`.16` wired / `.165` WiFi), key installed.
- **KVM `.39` → WHITESKELETON** (Win11), firmware V1.9.1.
- ATX unwired fleet-wide → reboots are OS-initiated; WoL is the only OOB power-on
  (hence the #199 `power on` → WoL fallback).

_Standing rules (issue-per-finding · direct commits to `main` · stdlib-only at
core import · `pip install` ships every surface · docs↔shipped parity · run
`healthcheck` first · never hand-edit the auto-generated wiki): see
[CLAUDE.md](CLAUDE.md)._
