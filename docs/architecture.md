# kvm-pilot architecture

> **Status: landed, still growing.** This started as the target design; the
> refactor has caught up with it — the capability protocols (Step 1), the driver
> registry + PiKVM family split (Step 3), and concrete Fake / Redfish / IPMI
> drivers (Step 4) are all in place. Step 2 (transport/auth split) and Step 5
> (entry-point plugins) are the remaining steps, tracked in the architecture epic.

`kvm-pilot` is built around a **driver-plugin** model so support can grow to
many KVM / BMC devices without reworking the core. The library API, CLI, safety
layer, and vision subsystem stay device-agnostic; each device is a *driver* that
implements only the capability protocols its hardware supports.

![kvm-pilot driver-plugin architecture](architecture.svg)

**Further reading:** [`redfish.md`](redfish.md) (Redfish driver reference — spec
grounding, portability rules, open questions) and [`decisions.md`](decisions.md)
(records of non-obvious design choices).

## Layers

- **Consumers** — the CLI, the `KVMClient` facade, and `ScreenAnalyzer`. None of
  them care which device is behind the driver.
- **Driver interface (the plugin seam)** — a set of small capability protocols.
- **Registry** — `make_driver(kind)` plus entry-point discovery and host
  autodetection (later step).
- **Drivers** — concrete implementations: the PiKVM family (`PiKVMDriver`,
  `GLKVMDriver`, `BliKVMDriver`), `RedfishDriver`, `IpmiDriver`, and a
  `FakeDriver` for tests; JetKVM etc. later.
- **Transport** — generic HTTP (urllib + retry + secret redaction), with room
  for WebSocket/JSON-RPC transports; the IPMI driver channels through the
  system `ipmitool` binary (subprocess) rather than an in-process transport.
- **Cross-cutting & shared** — `SafetyPolicy`, config, the error hierarchy, and
  the (already pluggable) vision backends, implemented once for every driver.

## Capability protocols

Rather than one monolithic interface, each feature area is a small,
`@runtime_checkable` `Protocol` in [`kvm_pilot.drivers.base`](../src/kvm_pilot/drivers/base.py):
`SystemInfo`, `Power`, `HID`, `Video`, `VirtualMedia`, `GPIO`, `Events`, the
sensing protocols `Logs`, `BootProgress`, `Sensors`, `SerialConsole`, and
`Watchdog` (the cheaper-than-vision signals — see the sensing model in the
README), plus the config protocol `BootConfig` (boot-source override — one-time
or persistent, #201). A driver implements only the ones it has and reports them via
`capabilities()`. Support is detected **structurally** — drivers never
hand-maintain a list:

```python
from kvm_pilot import KVMClient, Capability

kvm = KVMClient("192.168.8.1")
kvm.capabilities()            # -> {Capability.POWER, Capability.HID, ...}
kvm.supports(Capability.GPIO) # -> True / False
```

Devices differ widely, which is exactly why capabilities are segmented rather
than assumed:

| Capability | PiKVM / GLKVM / BliKVM | Redfish BMC | JetKVM | IPMI |
|---|---|---|---|---|
| Power | ✅ ATX | ✅ | ✅ | ✅ |
| HID (keyboard/mouse) | ✅ | ❌ (SOL console) | ✅ | ❌ |
| Video snapshot → vision | ✅ MJPEG | ❌ usually | ✅ | ❌ |
| Virtual media | ✅ | ✅ (many) | partial | ❌ |
| GPIO | ✅ | ❌ | ❌ | ❌ |
| Event stream | ✅ WebSocket | ⚠️ event svc | ✅ | ❌ |
| Logs | ✅ kvmd journal | ✅ SEL + lifecycle | ❌ | ✅ SEL |
| Boot progress | ❌ (vision) | ✅ structured enum | ❌ (vision) | ⚠️ POST codes |
| Sensors (temp/fan/watts) | ⚠️ Prometheus | ✅ | ⚠️ DC power | ✅ SDR/DCMI |
| Serial console (text) | ❌ unless wired | ✅ SOL | ❌ | ✅ SOL |
| Boot device control | ❌ (HID into firmware menus) | ✅ BootSourceOverride | ❌ | ✅ `chassis bootdev` |
| Watchdog | ❌ | ⚠️ | ❌ | ✅ |

`ScreenAnalyzer` already depends only on `Video` (it calls `snapshot_base64()`),
so vision works against any driver that captures frames — and is simply
unavailable on devices that don't.

## Transport & auth

The reusable parts of today's HTTP layer (retry/backoff, secret redaction,
urllib plumbing) become a generic `HttpTransport`; the PiKVM-specific pieces
(the `X-KVMD-*` auth headers, TOTP-in-password, the `ok`/`result` envelope)
become an `AuthStrategy` + response decoder. New transports (WebSocket JSON-RPC
for JetKVM, optional IPMI) slot in without touching driver code.

## Driver registry & families

`make_driver(kind, **conf)` mirrors the vision layer's `make_backend` — it
resolves `pikvm`/`glkvm`/`blikvm` (the PiKVM family), `redfish`, `ipmi`, `amt`, and
`fake` (the in-process `FakeDriver`), and `register_driver()` lets a third party
add a kind at runtime. The plan is for drivers to also register via a
`kvm_pilot.drivers` **entry-point group**, so a driver can ship as a separate
pip package without forking the core. `PiKVMDriver` holds the shared kvmd logic;
`GLKVMDriver` / `BliKVMDriver` subclass it and override only the deltas (Step 3
below).
The **Redfish** driver (`make_driver("redfish")`,
[`drivers/redfish/`](../src/kvm_pilot/drivers/redfish/)) is one DMTF-standard
stdlib client covering iDRAC, iLO, Supermicro, Lenovo XCC, and OpenBMC. It is portable
by *navigating hypermedia* — it follows `@odata.id` links and reads
`@Redfish.ActionInfo`/`AllowableValues` rather than hard-coding vendor ids or version
strings — and is session-auth-first (`X-Auth-Token`, `DELETE` on logout) with HTTP
Basic optional, reflecting the vendor shift away from Basic auth.
The **IPMI** driver (`make_driver("ipmi")`,
[`drivers/ipmi.py`](../src/kvm_pilot/drivers/ipmi.py), #62) covers BMCs that
predate Redfish (e.g. Dell iDRAC6): it shells out to the system `ipmitool`
(`-I lanplus`, password via env — never argv) and implements `Power`,
`SystemInfo`, `BootConfig`, `Sensors`, `Logs` (SEL), and `SerialConsole`
(SOL over a PTY — the `kvm-pilot console` CLI).
The **AMT / vPro** driver (`make_driver("amt")`,
[`drivers/amt/`](../src/kvm_pilot/drivers/amt/), #211) manages Intel-AMT laptops
and desktops out-of-band across *three* native protocols, all pure-stdlib: a
**WS-Man** SOAP client (Digest over 16992/16993, [`amt/wsman.py`](../src/kvm_pilot/drivers/amt/wsman.py))
for `Power` (CIM `RequestPowerStateChange`), `SystemInfo`, and single-use
`BootConfig`; **SOL** serial (`SerialConsole`, port 16994 via the battle-tested
`amtterm`, password via env); and **RFB / KVM-redirection** (`Video` + `HID`,
[`amt/rfb.py`](../src/kvm_pilot/drivers/amt/rfb.py)) — a from-scratch VNC client
(inline DES for the VNC challenge, since the stdlib has none) that captures the
**platform framebuffer**, i.e. a real BIOS/POST/GRUB screenshot on a machine
whose HDMI a capture-KVM never sees boot. It is the first non-PiKVM driver to
implement `Video`/`HID`, closing the seam Redfish leaves open.

## Safety

There is one `SafetyPolicy` / `DESTRUCTIVE_OPS` for the whole project. Op
identifiers today are device-namespaced after the kvmd API (`atx.power_off_hard`,
`msd.connect`, `gpio.switch`), and `FakeDriver` reuses those same ids verbatim;
every driver funnels destructive calls through the same `guard()`, so safety
semantics can't drift between devices. (Each family namespaces its own op ids
today — `redfish.set_boot_device`, `ipmi.serial_console`, `ssh.set_boot_next`;
a later step may rename them to driver-agnostic, capability-namespaced ids —
e.g. `power.off_hard`.)

## Migration

- [x] **Step 1 — capability protocols** ([`drivers/base.py`](../src/kvm_pilot/drivers/base.py)).
      `KVMClient` implements them via `CapabilityMixin`; no behaviour change.
- [ ] **Step 2** — split `http.py` into `HttpTransport` + `AuthStrategy`.
- [x] **Step 3 — driver registry + PiKVM family.** `make_driver(kind, **conf)` +
      `register_driver()` ([`drivers/__init__.py`](../src/kvm_pilot/drivers/__init__.py)),
      a `--driver` CLI flag, and `HostConfig.driver` (+ `KVM_PILOT_DRIVER`). `KVMClient`
      was split into a canonical `PiKVMDriver` base with fork subclasses:
      `GLKVMDriver` ([`drivers/glkvm.py`](../src/kvm_pilot/drivers/glkvm.py) — the GL
      fork diverges enough to own a module, #140) and `BliKVMDriver`
      ([`drivers/pikvm.py`](../src/kvm_pilot/drivers/pikvm.py));
      `KVMClient`/`PiKVMClient` stay as aliases. `GLKVMDriver` detects the GL
      "API disabled" 404 (→ `ApiDisabledError`), tracks per-firmware quirks, and
      carries GL's proprietary `/api/upgrade/*` flash layer.
      (Moving `PiKVMDriver` out of `client.py` into `drivers/pikvm/` is deferred.)
- [x] **Step 4 — drivers.** Four concrete non-PiKVM drivers have landed:
      `FakeDriver` ([`drivers/fake.py`](../src/kvm_pilot/drivers/fake.py)) — in-process,
      no hardware (#2) — `IpmiDriver` ([`drivers/ipmi.py`](../src/kvm_pilot/drivers/ipmi.py),
      #62 — pre-Redfish BMCs over `ipmitool`, live-validated on a Dell iDRAC6/R710
      including SOL) — `RedfishDriver`
      ([`drivers/redfish/`](../src/kvm_pilot/drivers/redfish/)), one stdlib client for
      Dell iDRAC / HPE iLO / Supermicro / Lenovo XCC / OpenBMC — and `AmtDriver`
      ([`drivers/amt/`](../src/kvm_pilot/drivers/amt/), #211 — Intel AMT/vPro over
      WS-Man + SOL + RFB). Redfish proves the
      capability seam: it advertises a *complementary* set
      (`SystemInfo`, `Power`, `BootProgress`, `Sensors`, `Logs`, `VirtualMedia`) and
      none of `HID`/`Video`/`GPIO`, so structured-state sensing (`BootProgress`,
      `Sensors`) finally has real implementers alongside the PiKVM pixels; the AMT
      driver then closes the other half of the seam — the first non-PiKVM driver to
      bring `Video` + `HID`, via firmware-level KVM redirection that screenshots the
      BIOS/GRUB a capture-KVM can't reach.
      `RedfishDriver` is on the CLI (`--driver redfish`) via **capability-aware
      `--driver` dispatch** ([#27](https://github.com/DustinTrap/kvm-pilot/issues/27)):
      a subcommand needing a capability the device lacks fails cleanly (exit 1)
      instead of `AttributeError`, and the `redfish` path is validated end-to-end
      against an external DMTF-conformant emulator (sushy-tools) in CI.
- [ ] **Step 5** — entry-point plugins + a "writing a driver" guide; per-driver deps as extras.

The zero-dependency stdlib core is preserved: the HTTP transport stays on
urllib, and a heavier per-driver dependency would ship as an optional extra —
the IPMI driver avoided needing one by shelling out to the system `ipmitool`
binary instead of pulling a Python IPMI stack.
