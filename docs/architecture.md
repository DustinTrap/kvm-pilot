# kvm-pilot architecture

> **Status: early alpha.** This describes the target design and tracks the
> incremental refactor toward it. **Step 1 (the capability protocols) has
> landed**; the remaining steps are tracked in the architecture epic.

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
- **Drivers** — concrete implementations: the PiKVM family today; Redfish,
  JetKVM, etc. later; a `FakeDriver` for tests.
- **Transport** — generic HTTP (urllib + retry + secret redaction), with room
  for WebSocket/JSON-RPC and IPMI transports.
- **Cross-cutting & shared** — `SafetyPolicy`, config, the error hierarchy, and
  the (already pluggable) vision backends, implemented once for every driver.

## Capability protocols

Rather than one monolithic interface, each feature area is a small,
`@runtime_checkable` `Protocol` in [`kvm_pilot.drivers.base`](../src/kvm_pilot/drivers/base.py):
`SystemInfo`, `Power`, `HID`, `Video`, `VirtualMedia`, `GPIO`, `Events`, plus the
sensing protocols `Logs`, `BootProgress`, `Sensors`, `SerialConsole`, and
`Watchdog` (the cheaper-than-vision signals — see the sensing model in the
README). A driver implements only the ones it has and reports them via
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

`make_driver(kind, **conf)` mirrors the vision layer's `make_backend` and is
**already in place** — it resolves `pikvm`/`glkvm`/`blikvm` (the API-compatible
`KVMClient` today) and `fake` (the in-process `FakeDriver`), and `register_driver()`
lets a third party add a kind at runtime. The plan is for drivers to also register
via a `kvm_pilot.drivers` **entry-point group**, so a driver can ship as a separate
pip package without forking the core. A dedicated `PiKVMDriver` will hold the shared
kvmd logic; `GLKVMDriver` / `BliKVMDriver` subclass it and override only the deltas.
The second device driver — **Redfish** (`make_driver("redfish")`,
[`drivers/redfish/`](../src/kvm_pilot/drivers/redfish/)) — **has landed**: one DMTF-standard
stdlib client covering iDRAC, iLO, Supermicro, Lenovo XCC, and OpenBMC. It is portable
by *navigating hypermedia* — it follows `@odata.id` links and reads
`@Redfish.ActionInfo`/`AllowableValues` rather than hard-coding vendor ids or version
strings — and is session-auth-first (`X-Auth-Token`, `DELETE` on logout) with HTTP
Basic optional, reflecting the vendor shift away from Basic auth.

## Safety

There is one `SafetyPolicy` / `DESTRUCTIVE_OPS` for the whole project. Op
identifiers today are device-namespaced after the kvmd API (`atx.power_off_hard`,
`msd.connect`, `gpio.switch`), and `FakeDriver` reuses those same ids verbatim;
every driver funnels destructive calls through the same `guard()`, so safety
semantics can't drift between devices. (A later step may rename them to
driver-agnostic, capability-namespaced ids — e.g. `power.off_hard` — once a
second device family lands.)

## Migration

- [x] **Step 1 — capability protocols** ([`drivers/base.py`](../src/kvm_pilot/drivers/base.py)).
      `KVMClient` implements them via `CapabilityMixin`; no behaviour change.
- [ ] **Step 2** — split `http.py` into `HttpTransport` + `AuthStrategy`.
- [x] **Step 3 — driver registry + PiKVM family.** `make_driver(kind, **conf)` +
      `register_driver()` ([`drivers/__init__.py`](../src/kvm_pilot/drivers/__init__.py)),
      a `--driver` CLI flag, and `HostConfig.driver` (+ `KVM_PILOT_DRIVER`). `KVMClient`
      was split into a canonical `PiKVMDriver` base with thin `GLKVMDriver` /
      `BliKVMDriver` subclasses ([`drivers/pikvm.py`](../src/kvm_pilot/drivers/pikvm.py));
      `KVMClient`/`PiKVMClient` stay as aliases. `GLKVMDriver` detects the GL
      "API disabled" 404 (→ `ApiDisabledError`) and tracks per-firmware quirks.
      (Moving `PiKVMDriver` out of `client.py` into `drivers/pikvm/` is deferred.)
- [x] **Step 4 — drivers.** Two concrete non-PiKVM drivers have landed:
      `FakeDriver` ([`drivers/fake.py`](../src/kvm_pilot/drivers/fake.py)) — in-process,
      no hardware (#2) — and `RedfishDriver`
      ([`drivers/redfish/`](../src/kvm_pilot/drivers/redfish/)), one stdlib client for
      Dell iDRAC / HPE iLO / Supermicro / Lenovo XCC / OpenBMC. Redfish proves the
      capability seam: it advertises a *complementary* set
      (`SystemInfo`, `Power`, `BootProgress`, `Sensors`, `Logs`, `VirtualMedia`) and
      none of `HID`/`Video`/`GPIO`, so structured-state sensing (`BootProgress`,
      `Sensors`) finally has real implementers alongside the PiKVM pixels.
      `RedfishDriver` is on the CLI (`--driver redfish`) via **capability-aware
      `--driver` dispatch** ([#27](https://github.com/DustinTrap/kvm-pilot/issues/27)):
      a subcommand needing a capability the device lacks fails cleanly (exit 1)
      instead of `AttributeError`, and the `redfish` path is validated end-to-end
      against an external DMTF-conformant emulator (sushy-tools) in CI.
- [ ] **Step 5** — entry-point plugins + a "writing a driver" guide; per-driver deps as extras.

The zero-dependency stdlib core is preserved: the HTTP transport stays on
urllib, and any heavier per-driver dependency ships as an optional extra (e.g.
`kvm-pilot[ipmi]`).
