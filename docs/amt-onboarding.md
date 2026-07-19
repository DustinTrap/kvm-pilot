# Bringing Intel AMT online — an onboarding runbook

The operator/agent guide to getting an Intel **AMT / vPro** box under kvm-pilot
management: what to expect, the critical steps in order, and the failure modes
that will otherwise cost you hours. For *how the driver is built* (the wire
protocols, CIM classes, RFB details), see the reference: [`amt.md`](amt.md).

> **Read the [Expectations](#expectations--read-this-first) and the
> [ME-firmware-update hazard](#-the-me-firmware-update-hazard-read-before-updating)
> before you touch anything.** They are the two things that surprise everyone.

---

## What AMT is

Intel AMT/vPro is out-of-band management baked into the chipset's **Management
Engine (ME)** — a service processor that runs *below* the OS, on standby power.
It works when the host is off, hung, or still in firmware. Unlike an HDMI-capture
KVM (GL/PiKVM), it can **see and drive BIOS / POST / GRUB** on a laptop, where
those frames never leave over HDMI and there's no USB pass-through.

kvm-pilot's AMT driver gives you, all pure-stdlib over AMT's native ports:

| You get | Over | Port |
|---|---|---|
| Power (on/off/reset), inventory, one-time boot override | WS-Man (HTTP Digest) | 16992 / TLS 16993 |
| Serial console (SOL) | AMT redirection via `amtterm` | 16994 / TLS 16995 |
| Virtual media — boot from a local ISO (IDE-R) | AMT redirection | 16994 / TLS 16995 |
| Screenshot (BIOS/POST/GRUB) + keyboard/mouse (KVM) | RFB / KVM-redirection | 5900 |

---

## Expectations — read this first

AMT is powerful but quirky. Calibrate before you rely on it:

- **Pre-boot keyboard (HID) may not take effect on laptops.** On the validation
  unit (Dell Latitude 5411) the ME *accepts* keystrokes but the firmware never
  consumes them at BIOS/GRUB — video works, keyboard does not. **Test it; don't
  assume.** If you must drive firmware menus, you may still need a physical
  keyboard. (For booting alternate media without a keyboard, use **IDE-R** or
  **PXE** instead.)
- **Video captures *graphical* screens only** — BIOS / POST / GRUB / a GUI — **not
  legacy VGA text mode** (AMT resets instead of sending a text-mode frame). On
  some laptops it also won't render the OS's own framebuffer (e.g. the GDM login),
  so `snapshot` returns the idle overlay once the OS is up.
- **KVM is single-session** and prompts for **on-screen user consent** unless you
  disable it — and disabling consent needs **Admin Control Mode** (see below).
- **SOL connects, but only shows content if the platform redirects its console to
  serial.** Server BIOSes do; most laptops do not — so SOL is "connected, silent."
- **Plaintext by default.** 16992/16994 are unencrypted (HTTP Digest protects only
  the password hash). Use TLS (16993/16995) or keep the ME on an isolated
  management VLAN.
- **The AMT IP is usually the host's IP.** The ME shares the host NIC/MAC — one
  address answers both the OS (e.g. SSH) and AMT (16992). There's no separate
  "AMT IP" on most laptops.

---

## Part 1 — Provision AMT (from bare metal, at the machine)

*General OEM guidance — provisioning specifics vary by vendor, and this part was
not exercised in the kvm-pilot live runs (our unit came provisioned). If your box
already answers on 16992, skip to [Part 2](#part-2--bring-it-online-with-kvm-pilot-already-provisioned).*

Provisioning is a **physical, at-the-keyboard** step — there's no remote shortcut
for a factory-fresh ME.

1. **Enter MEBx.** Power on and press the ME setup hotkey — commonly
   **`Ctrl+P`** during POST, or `F12` → *MEBx* on Dell. Default password is
   `admin`; you're forced to set a new one (**8–32 chars, upper + lower + digit +
   special** — the same complexity AMT enforces everywhere).
2. **Enable AMT / Manageability** (the "Intel(R) ME" / "AMT Configuration" menu).
3. **Choose the control mode:**
   - **Admin Control Mode (ACM)** — full remote control, and the *only* mode that
     lets you **disable KVM user-consent** (silent, headless access). Preferred
     for a lab bench or fleet you control.
   - **Client Control Mode (CCM)** — easier to set up but **forces the on-screen
     consent prompt** for KVM, so a human must read a 6-digit code off the screen.
     Fine for attended desktops; painful for headless.
4. **Network access:** enable "Activate Network Access", pick DHCP or static, and
   note the address. Leave "Manageability Feature Selection" on.
5. **Save & exit.** The ME reboots; 16992 should now answer.

> Fleet-scale provisioning (host-based `rpc-go`/`acmactivate`, USB `Setup.bin`,
> or a provisioning server) is out of scope here — MEBx is the reliable
> one-machine path.

---

## Part 2 — Bring it online with kvm-pilot (already provisioned)

### Prerequisites
- `kvm-pilot` installed (`pip install --pre kvm-pilot`).
- `amtterm` on `PATH` for SOL (`brew install amtterm` / `apt install amtterm`).
- Network reachability to the AMT ports (test `nc -vz <host> 16992`).

### Step 1 — Add a config profile
`~/.config/kvm-pilot/config.toml` (chmod 600 — it holds a password):

```toml
[hosts.mybox]
host = "10.0.1.203"
user = "admin"
driver = "amt"
passwd = "…"                # the ME/MEBx admin password
amt_port = 16992            # 16993 if you provisioned TLS
amt_tls = false
amt_kvm_password = "Abcd1@ef"   # KVM/RFB password — EXACTLY 8 chars, upper/lower/digit/special
```

The **`amt_kvm_password` is separate** from the ME admin password and must be
**exactly 8 characters** with complexity, or `enable-kvm` fails with an opaque
fault.

### Step 2 — Healthcheck (the intake gate — run this FIRST)

```bash
kvm-pilot healthcheck --profile mybox
```

It verifies reachability + auth, provisioning state and control mode, which
redirection listeners are on, and prints the known firmware quirks. Treat a clean
healthcheck as the gate before anything destructive. (It also warns about
plaintext transport and consent-off posture — that's expected, not a failure.)

### Step 3 — Enable the redirection listeners

Provisioning turns AMT *on*, but the network **listeners** for SOL/IDE-R and KVM
are toggled separately (and **reset by ME firmware updates** — see below), so
this is the step people forget:

```bash
kvm-pilot amt enable-sol --profile mybox                 # opens 16994 (SOL + IDE-R)
kvm-pilot amt enable-kvm --profile mybox                 # opens 5900 (video + HID), consent ON
kvm-pilot amt enable-kvm --no-consent --profile mybox    # …or silent (needs Admin Control Mode)
```

- **`enable-sol` is required for both the serial console AND virtual media** —
  IDE-R rides the same 16994 channel.
- `enable-kvm --no-consent` only works in **ACM**; in CCM it's rejected (consent
  is mandatory there).

### Step 4 — Verify capabilities

```bash
kvm-pilot capabilities --profile mybox
# system_info, power, hid, video, boot_config, serial_console, virtual_media
```

You're online. Everything below is now available.

---

## What you can do now

```bash
kvm-pilot info        --profile mybox     # inventory (make/model/serial/AMT version/power)
kvm-pilot power       on|off|reset --profile mybox
kvm-pilot boot-device pxe|cd|hdd --profile mybox   # ONE-TIME override (AMT's model)
kvm-pilot snapshot    bios.png   --profile mybox   # BIOS/POST/GRUB screenshot
kvm-pilot console     --profile mybox              # interactive SOL (if the platform redirects serial)
kvm-pilot type "text" --profile mybox              # keyboard (verify it takes effect — see Expectations)
kvm-pilot mount fedora.iso --profile mybox         # attach an ISO as a virtual CD (IDE-R, #213)
```

**Boot a host from a local ISO (IDE-R)** — the no-physical-media path
(added in [#213](https://github.com/DustinTrap/kvm-pilot/issues/213); see
`amt.md`):

```bash
kvm-pilot amt enable-sol --profile mybox   # 16994 listener must be up
kvm-pilot mount fedora.iso --profile mybox # serve the ISO as a virtual CD
kvm-pilot boot-device cd   --profile mybox # next boot -> CD
kvm-pilot power reset      --profile mybox # boot into the ISO
```

The image streams live from your machine and the session stays open while the
host boots — keep the process running until the installer/OS is up. Legacy
`amtider` does **not** work on AMT ≥ 11; kvm-pilot speaks the modern redirection
protocol.

---

## ⚠️ The ME-firmware-update hazard (read before updating)

This is the single most important operational fact about AMT, learned the hard
way:

**Updating the ME/CSME firmware (e.g. a BIOS capsule via `fwupd`) resets the
redirection listeners AND can wedge the entire AMT management plane.** After the
update the ME may accept TCP on 16992 but time out every WS-Man call
(`HTTP 500 e:TimedOut`), return null inventory, and refuse to re-open the 16994
listener.

- **A warm reboot does NOT fix it. Neither does S3 suspend/resume.** The ME needs
  a full **G3 power cycle**: unplug AC (and on a laptop, drain flea power — hold
  the power button ~30 s, or disconnect the internal battery). Only that
  re-initializes the ME.
- **Heavy reset/session churn can re-wedge it** even after recovery. If AMT starts
  timing out or refusing during a firmware-adjacent workflow, stop hammering it
  and power-cycle. (Our IDE-R validation took two G3 cycles.)
- **After any firmware change, re-run `enable-sol` / `enable-kvm`** — the listeners
  were reset to off.

If you're updating firmware *specifically to gain a capability* (e.g. "newer
firmware for virtual media"), confirm it's actually a firmware gap first —
virtual media over AMT ≥ 11 was never a firmware limitation, it's client tooling.

---

## Troubleshooting (symptom → cause → fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| Every WS-Man call times out (`HTTP 500 e:TimedOut`); null inventory | ME wedged after a firmware update / reset churn (#217) | Full **G3 power cycle**, then `enable-sol`/`enable-kvm` |
| `enable-kvm` → `HTTP 400`; 5900 stays closed | KVM redirection reset by a firmware update, or ME not ready | Power-cycle; if it persists, re-check via MEBx that KVM redirection is enabled |
| `power`/`info` reports **off** while the OS is clearly up (SSH open) | ME degraded/still initializing — power *read* is stale | Power *control* often still works; give the ME time or power-cycle |
| Keystrokes have no effect at BIOS/GRUB | Pre-boot HID not effective on this platform (some laptops) | Use **IDE-R** or **PXE** for media; physical keyboard for MEBx |
| `snapshot` returns a blank / idle overlay | Display asleep, in text mode, or the OS framebuffer (not captured) | Send a key to wake it; remember AMT captures *graphical* firmware screens only |
| Box unreachable — no ping, no AMT, no SSH | Host idle-suspended (GNOME-on-AC will do this) | Send **Wake-on-LAN** first; then disable idle-suspend on always-on hosts (`systemctl mask sleep.target …`) |
| `boot-device bios` rejected | Boot-to-BIOS-setup is firmware-dependent (#215) | Boot a source instead (`pxe`/`cd`/`hdd`) |
| Virtual media: `amtider` fails (CONNECT→ERROR) on AMT 14 | Legacy IDE-R protocol revision | Use kvm-pilot's built-in `mount` (IDE-R, #213) |
| SOL connects but shows nothing | Platform doesn't redirect its console to serial (most laptops) | Expected; use `snapshot` for firmware screens instead |

---

## Security posture

- **Transport:** plaintext (16992/16994) by default. For anything beyond a trusted
  lab LAN, provision AMT for **TLS** (`amt_tls = true`, ports 16993/16995) or
  isolate the ME on a **management VLAN**.
- **KVM consent:** `--no-consent` gives silent view/control to anyone with the
  credentials — a real tradeoff. **Leave consent ON** unless the box is a
  controlled bench you accept that on.
- **Credentials:** keep the config file `chmod 600`; the ME admin password and the
  8-char RFB password are distinct. Never pass either on argv (visible in `ps`).
- **Disabling AMT forfeits your only firmware-level remote console** on a laptop —
  weigh that before turning it off for "security"
  ([#212](https://github.com/DustinTrap/kvm-pilot/issues/212)).

---

## See also
- [`amt.md`](amt.md) — the driver/protocol reference (WS-Man, RFB 4.0, IDE-R internals).
- [Troubleshooting & FAQ](troubleshooting.md) · [Configuration](configuration.md) · [CLI reference](cli.md).
- [Unattended Linux installs](unattended-install.md) — text-mode + SSH beats driving a graphical installer over KVM HID.
