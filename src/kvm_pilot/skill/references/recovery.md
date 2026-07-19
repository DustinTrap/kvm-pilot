# Recovery playbook — remote before physical

> Part of the bundled kvm-pilot skill. Read this **the moment** a host goes
> dark, wedged, or unreachable, or a snapshot fails or can't be trusted — a
> fresh read at need-time beats working from a faded memory of it. Also served
> at runtime by the MCP `doctrine` tool (topic "recovery").

When the host is wedged or its screen is black and you can't power-cycle it
through the KVM (`recovery-path` is CRITICAL — no ATX/GPIO wired), do **not**
jump to asking the user to physically intervene.

**First, read the symptoms.** *No video signal (`hdmi_signal` false) **and** HID not
reaching the target **and** the host doesn't answer on the network* is the signature
of a machine that **suspended or powered off on idle** (GNOME/Fedora auto-suspend is
a common cause) — not a KVM fault. A stale/`incomplete` ARP entry for its last-known
IP (`arp -a`) confirms it was up recently and is now down.

Prefer remote recovery, in this order:
1. **Wake-on-LAN — try this FIRST; it's cheap, low-risk, and non-invasive.** A
   single UDP magic packet does nothing if the host is already up or WoL is off, and
   wakes it instantly if it merely suspended (the common case above). Get the MAC
   from `arp -a`; broadcast `6×0xFF` + `MAC×16` to UDP ports 9 and 7, then poll
   `hdmi_signal` and ping for ~60–100 s. **Do not** burn time on HID re-enumeration
   or appliance restarts before ruling WoL out — it is a diagnostic, not a last
   resort.
2. **SSH into the target host OS** (in-band) — once it's awake and on the network,
   this is the fastest, most reliable lever (far better than typing through KVM HID).
   Probe with `ssh_reachable` / `ssh-check`, then act with `ssh_exec` / `ssh-exec`.
   **Ask the user for the target's IP / hostname / FQDN** (`ssh_host`) — it's a
   different machine from the KVM; note its DHCP lease can change across a reinstall.
3. **Clear a KVM-side fault** — `recover-hid` (HID gadget), then an **appliance
   reboot** (SSH) for a wedged encoder — but only once WoL/ping have shown the host
   is actually up while video/HID are stuck.
4. **On a business Intel laptop/desktop, use Intel AMT/vPro** (`--driver amt`) — the
   out-of-band lever *below* the OS, independent of the capture-KVM. It gives power
   (reset the wedged host), a firmware **BIOS/POST/GRUB screenshot** the HDMI-capture
   KVM can't see on a laptop, and SOL — precisely the pre-boot surface a capture-KVM
   is blind to. Provision AMT in MEBx first; then `amt enable-sol`/`enable-kvm` open
   the listeners over WS-Man with no further MEBx trip.
5. Only after remote options are exhausted, suggest **physical intervention**
   (press the power button) or **wiring the ATX cable** for future remote control.

> **Keep managed hosts awake.** GNOME/Fedora Workstation auto-suspends on idle (even
> at the login screen), which drops video, HID, *and* the network at once. On any
> host that must stay remotely reachable (e.g. one you'll manage via Cockpit/SSH),
> disable idle suspend as part of intake — `systemctl mask sleep.target
> suspend.target hibernate.target hybrid-sleep.target`.

> **Network sweep is opt-in and risky.** If the user doesn't know the target's
> address, you may *offer* to scan a network range for SSH — but say plainly it's
> noisy and only acceptable on networks they own, get them to confirm the range
> first, and never sweep by default.

## Reading a failed `snapshot`

- **HTTP 503 / "Service Unavailable"** → the video subsystem is down. Pull `logs`
  and look for encoder errors; a stuck encoder often clears with an **appliance
  reboot** (SSH).
- **A tiny/empty frame while `has_video_signal` is True** → the JPEG path can't
  encode the current mode, typically **H.264 at the panel's native resolution**.
  Use the WebRTC stream, or drop the host to 1080p, to see the screen.
- **A black/blank screen while `power_state`/`powered_on` reads True** → on a
  device whose capability profile marks power readings **not trusted** (no ATX
  board), `powered_on: true` can be an HDMI/EDID artifact, not proof the OS is up —
  `is_powered_on` fails *open*. **Don't trust it.** Disambiguate by what the
  snapshot actually shows **and** an **SSH reachability check to the target host**
  (is its OS answering on the network?), not "verify visually" alone — visual
  checks are exactly what fails on a black screen.

Symptom-first fixes for these and more (GLKVM API 404, approval cancel,
dark-host recovery):
[Troubleshooting & FAQ](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/troubleshooting.md).
