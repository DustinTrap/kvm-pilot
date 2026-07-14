# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-14 · `main @ 556b7ae` · **v0.1.0b6 released to PyPI**; IPMI driver + ipmi_sim cross-check landed on `main` **post-b6 (unreleased)**; `ipmisim` test VM now on the LAN via DHCP (`10.0.1.152`)

## Current state
The **remote boot-control + Wake-on-LAN epic (#200) shipped as v0.1.0b6**, and an
**IPMI driver (#62)** landed after it — the pre-Redfish BMC path for the R710.

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
- **POST-b6 on `main`, NOT yet released** (next beta bundles these):
  - **IPMI driver (#206 · `6f01ce7`)** — `drivers/ipmi.py` shells out to
    `ipmitool -I lanplus` (password via `IPMI_PASSWORD` env, never argv). Power /
    SystemInfo / BootConfig / Sensors / Logs → `power`/`boot-device`/`sensors`/
    `logs` work with no new CLI/MCP code. 34 hardware-free tests. IPMI completeness
    now ≈ Redfish minus VirtualMedia. **This is the R710/iDRAC6 (no-Redfish) path.**
  - **ipmi_sim cross-check (#207 · `02a5e73`)** — `tests/integration/test_ipmi_external.py`
    + env-gated `ipmi_bmc` fixture. Ran **6/6 green** against an independent
    OpenIPMI `ipmi_sim` BMC (answers as MontaVista, not the Dell fixtures) on a
    Fedora VM stood up on the homelab SNO cluster. Stock-sim limits documented in
    `docs/decisions.md`. CI all-green; merged.
- CHANGELOG has **no `[Unreleased]` section yet** for #206/#207 — add one before the
  next release.

## Next steps
- **Real-BMC validation (#29) — the big owed item, planned for tonight.**
  Runbook: [`docs/hardware-test-plan-ilo-idrac.md`](docs/hardware-test-plan-ilo-idrac.md).
  - **DL380 G9 = iLO4** (HAS Redfish, quirky) → full Redfish boot-device + power test.
  - **R710 = iDRAC6** (NO Redfish, predates it) → expect clean 404, use the new
    **IPMI** driver (`--driver ipmi`; iDRAC default `root`/`calvin`).
  - Blocked on the operator providing iLO/iDRAC IPs + creds.
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
