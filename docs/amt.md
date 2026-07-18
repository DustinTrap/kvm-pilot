# AmtDriver — Intel AMT / vPro reference

Implementation notes for [`drivers/amt/`](../src/kvm_pilot/drivers/amt/) (#211),
grounded in Intel's AMT SDK (WS-Man / CIM classes), the RFB/VNC protocol spec,
and the `amtterm` SOL client.

> **Status:** **mock-tested only — not yet run against real hardware.** Every
> path is covered by pure-stdlib emulators (WS-Man SOAP + an RFB server) and a
> DES FIPS-46-3 known-answer vector, but the driver has **not** been live-validated:
> AMT network access still has to be activated in MEBx on the test unit
> (Manageability → Activate Network Access → DHCP). Don't claim more than the
> [support matrix](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> shows. Sources at the bottom.

## Why AMT

Intel AMT/vPro is firmware-level out-of-band management baked into the chipset of
business Intel laptops and desktops. It matters here for one reason a
capture-KVM (GL/PiKVM on the HDMI port) can't cover: on a laptop the KVM is blind
to **BIOS/POST/GRUB** — those frames never leave over HDMI on many machines, and
there's no physical keyboard/mouse pass-through. AMT sees and drives the platform
*below* the OS, so `snapshot` returns a real BIOS screenshot and `type`/`press`
reach the firmware setup screens.

## Three protocols, one driver

AMT exposes distinct wire protocols on distinct ports; the driver speaks all
three, all **pure-stdlib** (no `python-amt`, no external VNC lib):

| Capability | Protocol | Port | Module |
|---|---|---|---|
| `Power`, `SystemInfo`, `BootConfig` | WS-Man (SOAP 1.2 over HTTP Digest) | 16992 (TLS 16993) | [`wsman.py`](../src/kvm_pilot/drivers/amt/wsman.py) |
| `SerialConsole` (SOL) | AMT serial-over-LAN via `amtterm` | 16994 | [`driver.py`](../src/kvm_pilot/drivers/amt/driver.py) |
| `Video` + `HID` | RFB / KVM-redirection (VNC) | 5900 (standard-port) | [`rfb.py`](../src/kvm_pilot/drivers/amt/rfb.py) |

### WS-Man (power / inventory / boot)

`wsman.py` is a minimal SOAP client — it builds WS-Addressing + WS-Man envelopes
and POSTs them over `urllib` with HTTP **Digest** auth, parsing responses with
`ElementTree` (namespace-agnostic local-name matching). Verbs: `get`,
`enumerate` (Enumerate + Pull), `invoke`, `put`.

- **Power** — read `CIM_AssociatedPowerManagementService.PowerState`; write
  `CIM_PowerManagementService.RequestPowerStateChange`. CIM state codes:
  on `2`, soft-off `8`, hard-off `6`, master-bus-reset `10`.
- **SystemInfo** — `CIM_Chassis` / `CIM_ComputerSystemPackage` (manufacturer,
  model, serial), `CIM_SystemPackaging`/UUID, `AMT_SetupAndConfigurationService`
  (AMT version, provisioning state). `get_info` is best-effort: a fault in one
  field never blanks the rest.
- **BootConfig** — **single-use only** (AMT's model). `set_boot_device` writes a
  `CIM_BootConfigSetting.ChangeBootOrder` pointing at a `CIM_BootSourceSetting`
  ("Intel(r) AMT: Force PXE/Hard-drive/CD Boot"), then `SetBootConfigRole` role 1
  to mark it one-shot; `bios` flips `AMT_BootSettingData.BIOSSetup`. Persistent
  boot (`once=False`) and `usb`/`diag` targets are rejected up front.

### SOL serial console

`SerialConsole` shells out to **`amtterm`** (like the IPMI driver shells out to
`ipmitool`) rather than reimplementing the AMT redirection framing. `kvm-pilot
console` drops into an interactive SOL session; `serial_read`/`serial_write` back
a PTY-driven session for scripted use. The password is passed via the
`AMT_PASSWORD` environment variable — **never** on argv (so it can't leak via
`ps`). Missing `amtterm` raises `CapabilityError` with an install hint.

### RFB / KVM-redirection (video + HID)

`rfb.py` is a from-scratch RFB 3.8 client. Two things the stdlib doesn't give us:

- **VNC auth needs DES**, which Python's stdlib dropped — so `rfb.py` carries a
  small, self-contained DES (verified in tests against the **FIPS 46-3**
  known-answer vector `key=0123456789ABCDEF, pt=4E6F772069732074 →
  ct=3FA40E8A984D4815`). The VNC quirk of bit-reversing each password byte into
  the DES key is handled inline.
- **PNG encoding** — the RAW framebuffer (BGRA) is converted to RGBA and encoded
  to PNG with `zlib` + `struct` + `crc32`, no Pillow.

`snapshot()` opens a short-lived redirection session and returns the platform
framebuffer as PNG; `type_text`/`press_key`/`send_shortcut`/`mouse_*` reuse a
persistent HID session, translating names via an X11 keysym map.

## Configuration

```toml
[hosts.laptop]
driver = "amt"
host   = "10.0.1.20"
user   = "admin"
# passwd via KVM_PILOT_PASSWORD / config — the WS-Man + SOL credential
amt_port = 16992          # WS-Man; 16993 with amt_tls = true
amt_tls  = false
# amt_kvm_password: the *separate* MEBx KVM-redirection (RFB) password;
# falls back to `passwd` when unset. Env: KVM_PILOT_AMT_KVM_PASSWORD
```

The RFB (KVM-redirection) password is provisioned independently of the WS-Man
admin password in MEBx, so it's a distinct field; when you haven't set a separate
one, the driver reuses the WS-Man password.

## Safety

Every state-changing op is gated through the one project `SafetyPolicy`:
`amt.power_on` / `amt.power_off` / `amt.power_off_hard` / `amt.reset_hard`
(power), `amt.set_boot_device` (config), `amt.serial_console` (SOL), and the
shared `hid.type_text` / `hid.press_key` / `hid.send_shortcut` / `hid.mouse_click`
for RFB input. Reads (`is_powered_on`, `get_info`, `get_boot_options`,
`snapshot`, `mouse_move`) are ungated. `dry_run=True` / a `confirm` returning
`False` short-circuits every write with no bytes on the wire.

## Live-bring-up checklist

1. In MEBx (Ctrl-P at boot): set an MEBx password, **Manageability = Enabled**,
   **Activate Network Access**, **Network → DHCP**.
2. Confirm the ports are open: `nc -vz <host> 16992` (WS-Man),
   `16994` (SOL), `5900` (KVM redirection — must be **standard-port** enabled).
3. `kvm-pilot healthcheck --driver amt --host <host> --user admin` (intake gate).
4. `kvm-pilot info` → `snapshot` (BIOS screenshot) → gated `power` / `console`.

## Sources

- Intel AMT SDK — WS-Man / CIM class reference (`CIM_PowerManagementService`,
  `CIM_BootConfigSetting`/`CIM_BootSourceSetting`, `AMT_BootSettingData`,
  `AMT_SetupAndConfigurationService`).
- DMTF WS-Management (DSP0226) and WS-CIM (DSP0227) for the SOAP framing.
- The RFB Protocol (RFC 6143) — VNC handshake, VNC authentication (DES), RAW encoding.
- FIPS PUB 46-3 — DES known-answer test vector.
- `amtterm` (the `amtterm`/`amttool` suite) for the SOL client contract.
