# IPMI onboarding — bringing a pre-Redfish BMC online

> **Operator runbook** for IPMI 2.0 BMCs — iDRAC6, older iLO, Supermicro, and
> any BMC whose Redfish service is absent or disabled. The capability reference
> is [`driver-features.md`](driver-features.md#ipmi--ipmi-20-bmcs-pre-redfish-idrac--ilo--supermicro);
> this is the ordered path to a working device, seeded from a live Dell
> PowerEdge R710 / iDRAC6 1.95 bring-up.

---

## Expectations — read this first

**There is no video. At all.** IPMI has no framebuffer — `video_scope` is
`none`. This is the difference that catches people out: you can power the
machine, choose its boot device, read its sensors and its event log, and watch
its **text** console over SOL — but you cannot take a screenshot, and no amount
of configuration will change that. Pixels on a BMC come from the vendor's own
KVM, which is outside IPMI entirely.

What follows from that:

- **Firmware screens are reachable only as text**, over Serial-over-LAN, and
  only if the platform redirects its console to serial. Server BIOSes usually
  do; laptop firmware usually does not.
- **A graphical installer is not drivable this way.** Plan a text-mode install
  (kickstart / preseed) or use a different interface.

## Prerequisites

- **`ipmitool` on `PATH`** — the driver shells out to it rather than carrying a
  Python IPMI stack. Missing, you get a clear `CapabilityError` naming the
  package, not an obscure failure.
  - Debian/Ubuntu `apt install ipmitool` · RHEL/Fedora `dnf install ipmitool` ·
    macOS `brew install ipmitool`
- The BMC's IP, and a BMC account (**not** an OS account).
- UDP **623** reachable. It is UDP, so silence is ambiguous — a firewall drop
  looks identical to a powered-down BMC.

## 1 — Write a profile

```toml
[hosts.oldserver]
host = "10.0.0.169"
driver = "ipmi"
user = "root"
# passwd via KVM_PILOT_PASSWD
# ipmi_cipher = 3     # only if the BMC rejects the negotiated suite
```

`driver` is optional — auto-detection sends an RMCP/ASF presence ping, the one
IPMI exchange needing no credentials, and identifies the BMC from its pong
(#235).

Older BMCs are fussy about the RAKP cipher suite. If authentication fails
oddly, pin `ipmi_cipher = 3` (or 17) before assuming the password is wrong.

## 2 — Run the intake gate

```bash
kvm-pilot healthcheck --profile oldserver
```

On a healthy BMC expect `api-reachable` OK, `recovery-path` **OK** (a BMC's
reset is genuine out-of-band power — unlike a capture-KVM with unwired ATX), and
`preboot-video` INFO explaining there is no video here.

## 3 — Confirm identity and power

```bash
kvm-pilot info --profile oldserver     # vendor/model from FRU
kvm-pilot power --profile oldserver --show
```

Identity comes from **FRU Board Product**, not the naive field — a real-hardware
fix (#62): the obvious one returns `localhost` on an R710 and wrote `fake/fake`
into ledger rows.

## What you can do now

| Want | Command |
|---|---|
| Power on/off/cycle/reset | `kvm-pilot power ...` — state read-back is **trustworthy** here |
| Next-boot device | `kvm-pilot boot-device pxe --profile oldserver` |
| Temps / fans / voltages | `kvm-pilot sensors --profile oldserver` |
| BMC event log (SEL) | `kvm-pilot logs --profile oldserver` |
| Text console (BIOS, GRUB, kernel) | `kvm-pilot console --profile oldserver` |

Unlike the GL capture-KVM, **the power state here can be believed** — verified
live by read-back on the R710 across off → on → reset.

## Hazards & troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ipmitool not found` | Prerequisite missing | Install it (above) |
| Auth fails with correct credentials | BMC rejects the negotiated RAKP cipher | Pin `ipmi_cipher = 3` (or 17) |
| No response at all | UDP 623 filtered, or BMC powered down | It is UDP — silence is ambiguous. Check from the BMC's own subnet |
| `set_boot_device("usb")` rejected | **IPMI has no `usb` selector** | Use `floppy` — IPMI's removable-media target, and what a read reports (#243) |
| SOL connects but shows nothing | Platform not redirecting console to serial, or wrong COM port | **iDRAC6 SOL is COM2 (`ttyS1`)**, not COM1. Enable console redirection in BIOS |
| SOL shows binary noise | Baud mismatch between BIOS and BMC | Match the BIOS serial-redirect baud to the BMC's SOL setting |
| `sensors` returns nothing | BMC models no device SDRs | Real hardware populates this (105 rows on the R710); a stock `ipmi_sim` does not |
| Power verbs accepted, state never changes | Simulator without a modelled chassis | Expected on `ipmi_sim`; on real hardware the read-back is authoritative |

## Security posture

- **IPMI 2.0's RAKP handshake has known offline-password-cracking weaknesses.**
  Treat a BMC network as hostile-adjacent: put BMCs on an isolated management
  VLAN, never expose 623 to the internet, and do not reuse an OS password.
- Use a dedicated BMC account with only the privilege level you need — not the
  vendor default (`root`/`calvin` on Dell, `ADMIN`/`ADMIN` on Supermicro).
  `healthcheck`'s `default-creds` check flags those.
- The password never reaches argv — it rides the environment into `ipmitool`, so
  it is not visible in `ps`.
- SOL carries a live console: anyone who can open it can type at your BIOS.
  It is gated as an act with its own effect class for that reason.

## Evidence behind this page

Every claim above is either from the live **Dell PowerEdge R710 / iDRAC6 1.95**
run (2026-07-14 — power verified by read-back, boot-device round-trips, 105 SDR
rows, ~150 SEL entries, a full BIOS boot and F11 menu captured over SOL) or from
the OpenIPMI **`ipmi_sim`** cross-check. Where the two disagree, the difference
is called out above — the simulator's gaps are not the driver's.

## See also

- [`driver-features.md`](driver-features.md) — per-capability reliability and testing level
- [`redfish.md`](redfish.md) — if the BMC *does* speak Redfish, prefer that driver
- [`test-plan.md`](test-plan.md) · [`troubleshooting.md`](troubleshooting.md)
