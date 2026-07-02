# RedfishDriver — reference

Implementation notes for [`drivers/redfish/`](../src/kvm_pilot/drivers/redfish/),
grounded in the DMTF Redfish spec and Dell/HPE/Supermicro/Lenovo/OpenBMC docs.

> **Status:** alpha, **mock-tested only — never run against a real BMC.** Behaviors
> that can only be confirmed on hardware are tracked in [#29](https://github.com/DustinTrap/kvm-pilot/issues/29).
> Sources are listed at the bottom.

## What it is

One stdlib (`urllib`) client for server BMCs. It advertises a BMC's
*complementary* capability set — strong on structured state, no pixels:

| Capability | Redfish source |
|---|---|
| `SystemInfo` | `ComputerSystem` (`Manufacturer`/`Model`/`UUID`/`BiosVersion`/`PowerState`/`Status`) + `ServiceRoot.RedfishVersion` |
| `Power` | read `ComputerSystem.PowerState`; write `Actions/#ComputerSystem.Reset` |
| `BootProgress` | `ComputerSystem.BootProgress.LastState` → the project's phase vocabulary |
| `Sensors` | `Chassis/{id}/Sensors` (unified) **or** legacy `Chassis/{id}/Thermal` + `/Power` |
| `Logs` | a discovered `LogService` `Entries` collection (SEL / Lclog / IML) |
| `VirtualMedia` | a `VirtualMedia` slot + `InsertMedia`/`EjectMedia` actions |

**Not implemented** (a BMC has none): `HID`, `Video`, `GPIO`. **Deferred** ([#28](https://github.com/DustinTrap/kvm-pilot/issues/28)):
`SerialConsole` (SOL is an SSH/IPMI descriptor, not an HTTP byte stream),
`Events` (push/SSE), `Watchdog` (an IPMI primitive).

## The cardinal rule: navigate hypermedia, don't hard-code

Member ids are **not** portable — Dell `System.Embedded.1` / `iDRAC.Embedded.1`,
HPE / Supermicro / Lenovo `1`, OpenBMC `system` / `bmc`. So:

- **Discover by following `@odata.id`**, never assume an id. From `ServiceRoot`
  (`/redfish/v1/`) → `Systems`/`Chassis`/`Managers` collections → `Members[0].@odata.id`.
- **Read `Actions[...].target`** for action URIs; learn allowed parameters from
  `@Redfish.ActionInfo` (preferred) → inline `ResetType@Redfish.AllowableValues`
  → fall back to the DMTF enum and let the POST 4xx tell you. iDRAC10 drops the
  inline annotation; HPE keeps it.
- **Feature-detect on payload shape, not version strings.** Decide a feature
  exists because the property/link/action is present, not from `RedfishVersion`
  or `@odata.type` (vendors ship different minor versions per resource).
- **Page** every collection via `Members@odata.nextLink` (bounded — a cyclic
  nextLink must not loop forever).

## Power: map intent → the target's actual `ResetType`

`ResetType` support varies per target (and power state), so map each intent to
the first advertised value:

| method | preference order |
|---|---|
| `power_on` | `On` → `ForceOn` |
| `power_off` | `GracefulShutdown` → `PushPowerButton` → `ForceOff` |
| `power_off_hard` | `ForceOff` → `PushPowerButton` |
| `reset_hard` | `ForceRestart` → `PowerCycle` → `GracefulRestart` |

`reset_hard` prefers `ForceRestart` because **`GracefulRestart` is not reliably
graceful** (Dell iDRAC9 v3.36 bug; HPE iLO5 advisory). If none match, raise
`CapabilityError` with the advertised set.

**Current PowerState is read before every reset.** Two reasons: (1) if the host
is already at the intended state, no reset is issued at all — a redundant reset
is a no-op at best and a `400`/`409` on many BMCs (HPE iLO
`InvalidOperationForSystemState`); (2) `PushPowerButton` *pulses* the power
button (DSP0268) — a state toggle — so it is chosen only when the pulse moves
toward the target. On iDRAC8-class firmware, whose off set is `[ForceOff,
PushPowerButton]` with no `GracefulShutdown`, this means `power_off` on an
already-off host does nothing instead of powering it back **on**. If a reset is
nonetheless rejected with `400`/`409` but the host is observed at the target
state (a race), that is treated as success rather than an error.

## BootProgress → phase vocabulary

`BootProgressTypes` maps to `vision.base` phases: the `*Started`/`*Complete`
init states → `post_screen`; `SetupEntered` → `bios_menu`; `OSBootStarted` →
`booting`; `OSRunning` → `os_running` (a token added for exactly this). A literal
`None` + powered off → `power_off`; absent `BootProgress` → `None` ("can't
report"); unknown enum → `unknown` (never raise).

## Auth (session-first) and security

1. `GET /redfish/v1/` → `Links.Sessions.@odata.id` (else `/redfish/v1/SessionService/Sessions`).
2. `POST` it with **no auth header**, `{"UserName","Password"}` → capture
   `X-Auth-Token` (sent on every later request) + `Location` (the session URI).
3. `DELETE` the session on logout — BMCs cap sessions (Lenovo XCC ~16; iLO
   limited), so leaks lock you out. Fall back to the body `@odata.id` if
   `Location` is absent.

- **HTTP Basic** is optional (`auth="basic"`); vendors increasingly disable it
  (Dell flipped the default to *Unadvertised* in iDRAC9 7.30) — session-first is
  the right default.
- **`PasswordChangeRequired`**: a login can return 201 yet carry that MessageId
  with every op then 403 — surface a distinct `AuthError`, never auto-PATCH.
- **Pin credentials to the configured origin.** A BMC-supplied absolute URL
  (`Location`/`@odata.id`/Task monitor) pointing off-host must not receive the
  token / Basic auth.

## Async, errors, sensors, media, logs

- **Async:** an action may return `204` (sync) or `202` + a `Location` Task
  monitor — poll `TaskState` to a terminal state; treat **404/410 on the monitor
  as success** (iDRAC/iLO GC finished tasks) and `TaskStatus=Critical` as failure.
- **Errors:** parse `error.@Message.ExtendedInfo[*].MessageId` (read whichever of
  `Severity`/`MessageSeverity` is present). Map `401/403`→`AuthError`,
  `409`→`BusyError`, `503`→`UnavailableError`.
- **Sensors:** prefer the unified `Sensors` collection if the `Chassis` advertises
  the link; else legacy `Thermal` + `Power`.
- **VirtualMedia:** collect slots from **both** System- and Manager-level
  collections, pick by `MediaTypes` (`cdrom` flag), read `InsertMedia`/`EjectMedia`
  targets from the slot. Redfish has no separate "connect" step — Insert attaches.
  `InsertMedia` sends only `Image`: the optional `Inserted`/`WriteProtected`
  properties (DSP2046) are rejected by strict firmware (Supermicro; the fix
  sushy adopted), and a BMC that instead *requires* `TransferProtocolType`
  (400 `ActionParameterMissing`) gets one retry with it derived from the URL
  scheme.
- **Logs:** scan `LogServices` under both the System and the Manager (Dell SEL/Lclog
  under Manager; HPE IML under System); follow the `@odata.id` link, never build
  `/LogServices` by hand.

## Safety

Reset and virtual-media insert/eject route through `SafetyPolicy.guard()` with the
`redfish.*` ids in [`safety.py`](../src/kvm_pilot/safety.py). Reads are never gated.

## Open questions (need real hardware — [#29](https://github.com/DustinTrap/kvm-pilot/issues/29))

Sync-vs-async per vendor/action; current iDRAC9/10 & iLO5/6 `ResetType` sets;
ETag/If-Match enforcement (and empty-`""`-ETag handling) once boot-override lands
([#28](https://github.com/DustinTrap/kvm-pilot/issues/28)); `LogService` selection
on unseen vendors; whether HPE can disable Basic auth.

## Sources

- DMTF Redfish Specification **DSP0266** (1.15.x) and Data Model **DSP0268**; the
  schema bundle at <https://redfish.dmtf.org/schemas/> (`ComputerSystem`,
  `ComputerSystem.Reset`/`ActionInfo`, `BootProgress`, `Sensor`, `Thermal`/`Power`,
  `VirtualMedia`, `LogService`/`LogEntry`, `Task`, `SessionService`).
- Redfish error model: <https://redfish.redoc.ly/docs/concepts/errorresponses/>.
- Dell iDRAC Redfish API guide (dell.com/support manuals); HPE iLO RESTful/Redfish
  (developer.hpe.com); Supermicro Redfish; Lenovo XCC REST API
  (<https://pubs.lenovo.com/xcc-restapi/>); OpenBMC
  (<https://github.com/openbmc/docs/blob/master/REDFISH-cheatsheet.md>).
- `ResetType` AllowableValues drift: <https://redfishforum.com/thread/354>.
  Empty-ETag handling lesson: OpenStack Sushy release notes.
