# kvm-pilot architecture

> **Status: early alpha.** This describes the target design and tracks the
> incremental refactor toward it. **Step 1 (the capability protocols) has
> landed**; the remaining steps are tracked in the architecture epic.

`kvm-pilot` is built around a **driver-plugin** model so support can grow to
many KVM / BMC devices without reworking the core. The library API, CLI, safety
layer, and vision subsystem stay device-agnostic; each device is a *driver* that
implements only the capability protocols its hardware supports.

![kvm-pilot driver-plugin architecture](architecture.svg)

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
`SystemInfo`, `Power`, `HID`, `Video`, `VirtualMedia`, `GPIO`, `Events`. A driver
implements only the ones it has and reports them via `capabilities()`. Support is
detected **structurally** — drivers never hand-maintain a list:

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

`make_driver(kind, **conf)` mirrors the vision layer's `make_backend`. Drivers
register via a `kvm_pilot.drivers` **entry-point group**, so third parties can
ship a driver as a separate pip package without forking the core. `PiKVMDriver`
holds the shared kvmd logic; `GLKVMDriver` / `BliKVMDriver` subclass it and
override only the deltas (they are API-compatible forks). The highest-leverage
second driver is **Redfish** — a DMTF standard that covers iDRAC, iLO,
Supermicro, and OpenBMC in one implementation.

## Safety

There is one `SafetyPolicy` / `DESTRUCTIVE_OPS` for the whole project. Op
identifiers are driver-agnostic and capability-namespaced (`power.off_hard`,
`media.connect`, `gpio.switch`); every driver funnels destructive calls through
the same `guard()`, so safety semantics can't drift between devices.

## Migration

- [x] **Step 1 — capability protocols** ([`drivers/base.py`](../src/kvm_pilot/drivers/base.py)).
      `KVMClient` implements them via `CapabilityMixin`; no behaviour change.
- [ ] **Step 2** — split `http.py` into `HttpTransport` + `AuthStrategy`.
- [ ] **Step 3** — `PiKVMDriver` + `make_driver()` registry + `HostConfig.driver` + `--driver`.
- [ ] **Step 4** — `RedfishDriver` (server BMCs) + `FakeDriver` (local smoke tests, #2).
- [ ] **Step 5** — entry-point plugins + a "writing a driver" guide; per-driver deps as extras.

The zero-dependency stdlib core is preserved: the HTTP transport stays on
urllib, and any heavier per-driver dependency ships as an optional extra (e.g.
`kvm-pilot[ipmi]`).
