# AmtDriver — Intel AMT / vPro reference

Implementation notes for [`drivers/amt/`](../src/kvm_pilot/drivers/amt/) (#211),
grounded in Intel's AMT SDK (WS-Man / CIM classes), the RFB/VNC protocol spec,
and the `amtterm` SOL client.

> **Status:** **live-validated on a Dell Latitude 5411 (AMT 14.1.67).** WS-Man
> Power / SystemInfo / single-use BootConfig, remote SOL + KVM enablement, and a
> full **1920×1080 BIOS/POST screenshot over KVM redirection** were all exercised
> against real hardware (derived maturity **beta**); the SOL channel connects live
> via `amtterm`. The whole surface is also covered by pure-stdlib emulators (WS-Man
> SOAP + an RFB server), a DES FIPS-46-3 vector, and ZRLE tile-decode vectors. The
> [support matrix](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> is the source of truth. Sources at the bottom.
>
> **Honest live caveats** (all target/firmware-dependent, not driver bugs):
> - **Video** captures *graphical* screens (BIOS/POST/GRUB/GUI) but **not legacy
>   VGA text mode**; 5900 is absent on some AMT ≥12 SKUs.
> - **SOL** connects, but shows text only if the target redirects its console to
>   serial — server BIOSes do; the Latitude 5411 *laptop* BIOS does not, so the
>   channel is validated but there's no BIOS/OS serial content.
> - **`boot-device bios`** (boot-to-BIOS-setup) is firmware-dependent — rejected on
>   the Latitude 5411 with a clear error ([#215](https://github.com/DustinTrap/kvm-pilot/issues/215));
>   pxe/cd/hdd/none work.
> - **HID** wire format is verified against MeshCommander (standard RFB `KeyEvent`,
>   big-endian X11 keysyms) and emulator-tested, but live keystroke *effect* is
>   `unverified` — the test unit sits at a persistent Dell F1 firmware alert the
>   embedded controller services, which the emulated USB keyboard can't dismiss.

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

### Remote SOL / KVM enablement (no MEBx trip)

MEBx *provisions* SOL and KVM, but their network **listeners** can be toggled
over WS-Man — so `enable_sol()` and `enable_kvm()` open ports 16994 and 5900
remotely (this is how MeshCommander / Intel's rpc-go work). Both are gated.

- `enable_sol()` — full-object PUT to `AMT_RedirectionService`
  (`ListenerEnabled=true`, `EnabledState=32771` = IDER+SOL).
- `enable_kvm(require_consent=…)` — PUT `IPS_KVMRedirectionSettingData`
  (`Is5900PortEnabled=true`, `RFBPassword`, `SessionTimeout` non-zero),
  `CIM_KVMRedirectionSAP.RequestStateChange(2)`, and — for `require_consent=False`
  — `IPS_OptInService.OptInRequired=0` to drop the on-screen consent prompt.
  Consent-off needs **Admin Control Mode** (rejected in Client Control Mode).

AMT's WS-Transfer Put is strict: send the **whole** object (a partial or
reordered body is `InvalidRepresentation`), so the driver GET-modifies-PUTs the
full instance via one `_rmw_put` helper.

### RFB / KVM-redirection (video + HID)

The KVM video path is the fiddliest thing in the driver, because AMT's KVM server
is **Intel's proprietary RFB 4.0**, not stock VNC. The hard-won, live-validated
protocol (matching MeshCommander's decoder):

- **Version:** AMT announces `RFB 004.000`; the client must reply **`RFB 003.008`**
  (downgrade) — echoing 004.000 gets dropped.
- **Auth:** standard VNC (security type 2) needs **DES**, which the stdlib
  dropped — so `rfb.py` carries a self-contained DES (verified against the
  **FIPS 46-3** vector `key=0123456789ABCDEF, pt=4E6F772069732074 →
  ct=3FA40E8A984D4815`; VNC's per-byte bit-reversal of the password is inline).
  The **RFB password must be exactly 8 chars** with an upper/lower/digit/special
  — the driver validates this up front (AMT otherwise returns an opaque fault).
- **Pixel format:** AMT is **16-bpp RGB565**; the client sends **no**
  `SetPixelFormat` (a 32-bpp request makes AMT reset) and keeps the native format.
- **Encodings:** `SetEncodings` must **explicitly list RAW** (AMT doesn't assume
  it) plus **RLE(16)** and DesktopSize. Integrated/hybrid-GPU platforms refuse
  RAW and only deliver **RLE(16)** — AMT's ZRLE-style scheme over one **standard-
  zlib** stream (a `0x78 0x9c` header; *not* raw deflate) of ≤64×64 tiles. The
  full ZRLE sub-encodings (raw / solid / packed-palette / plain-RLE / palette-RLE)
  are decoded; RGB565→RGB888 via a precomputed LUT; PNG out via `zlib`/`crc32`.
- **Single session:** AMT allows one KVM session; a dropped one can wedge the
  port, so `snapshot()` cycles the SAP and retries.

`snapshot()` returns the platform framebuffer as PNG — a genuine BIOS/POST/GRUB
screenshot (validated live at 1920×1080); `type_text`/`press_key`/`send_shortcut`/
`mouse_*` reuse a persistent HID session with an X11 keysym map.

> **Capture limit:** AMT grabs *graphical* framebuffers (BIOS / POST / GRUB / a
> GUI) but **not legacy VGA text mode** — it resets right after the framebuffer
> request instead of sending a frame. A reset at that exact point means
> "unsupported display mode," not a driver bug.

## Configuration

```toml
[hosts.laptop]
driver = "amt"
host   = "10.0.1.20"
user   = "admin"
# passwd via KVM_PILOT_PASSWORD / config — the WS-Man + SOL credential
amt_port = 16992          # WS-Man; 16993 with amt_tls = true
amt_tls  = false
# amt_kvm_password: the *separate* KVM-redirection (RFB) password — must be
# EXACTLY 8 chars (upper+lower+digit+special), AMT's rule. Falls back to `passwd`
# when unset. Env: KVM_PILOT_AMT_KVM_PASSWORD
```

The RFB (KVM-redirection) password is provisioned independently of the WS-Man
admin password, so it's a distinct field; when unset the driver reuses the WS-Man
password — but note AMT requires the RFB password to be **exactly 8 characters**,
so a longer admin password won't work for KVM and must be set separately.

## Safety

Every state-changing op is gated through the one project `SafetyPolicy`:
`amt.power_on` / `amt.power_off` / `amt.power_off_hard` / `amt.reset_hard`
(power), `amt.set_boot_device` (config), `amt.serial_console` (SOL),
`amt.enable_sol` / `amt.enable_kvm` (open a management port — and `enable_kvm`
with `require_consent=False` disables the on-screen consent prompt), and the
shared `hid.type_text` / `hid.press_key` / `hid.send_shortcut` / `hid.mouse_click`
for RFB input. Reads (`is_powered_on`, `get_info`, `get_boot_options`,
`snapshot`, `mouse_move`) are ungated. `dry_run=True` / a `confirm` returning
`False` short-circuits every write with no bytes on the wire.

Two honesty notes baked into the driver: a pending **boot** source override is
write-only on AMT (`CIM_BootConfigSetting` returns no `BootOrder`), so
`get_boot_options()` reports `override_readable: false` rather than a misleading
`none`; and rapid WS-Man bursts trip AMT's **flood protection** (HTTP 401) — serialize calls and back off rather than treating it as an auth failure.

## Live-bring-up checklist

1. In MEBx (Ctrl-P at boot): set an MEBx password, **Manageability = Enabled**,
   **Activate Network Access**, **Network → DHCP**. Only 16992 (WS-Man) needs to
   be open from here — the rest can be turned on remotely (step 3).
2. `kvm-pilot healthcheck --driver amt --host <host> --user admin` (intake gate);
   `kvm-pilot info` confirms identity + power over WS-Man.
3. Enable the other channels over WS-Man (no MEBx trip): `kvm-pilot amt enable-sol`
   opens 16994; `kvm-pilot amt enable-kvm` opens 5900 (set an 8-char
   `amt_kvm_password` first). Also available as the MCP `amt_enable` tool and the
   library `enable_sol()` / `enable_kvm()` methods.
4. `snapshot` (BIOS/POST/GRUB screenshot — a *graphical* screen) → gated `power` /
   `console` / `boot-device`.

## Sources

- Intel AMT SDK — WS-Man / CIM class reference (`CIM_PowerManagementService`,
  `CIM_BootConfigSetting`/`CIM_BootSourceSetting`, `AMT_BootSettingData`,
  `AMT_SetupAndConfigurationService`).
- DMTF WS-Management (DSP0226) and WS-CIM (DSP0227) for the SOAP framing.
- The RFB Protocol (RFC 6143) — VNC handshake, VNC authentication (DES), RAW encoding.
- FIPS PUB 46-3 — DES known-answer test vector.
- `amtterm` (the `amtterm`/`amttool` suite) for the SOL client contract.
