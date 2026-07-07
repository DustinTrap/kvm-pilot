# Unattended Linux installs — text mode + SSH

Driving a graphical OS installer through KVM HID is inherently fragile:
coordinate mapping varies by device, resolution, and scaling (the #128/#129
field report), clicks land on the wrong elements, and every answer costs a
screenshot–classify–click round-trip. Text-mode installers, by contrast, are
fully keyboard-driven and SSH-accessible — the reliable happy path is to append
the distro's SSH + text-mode boot arguments at the bootloader, then finish the
install over SSH. The expensive HID phase sets up the cheap phase (the #81
actuation hierarchy): only the bootloader edit has to ride the KVM.

> **Honesty note.** This page distills each distro's *documented* remote-install
> mechanism for the KVM context; kvm-pilot has **not** exercised every row
> end-to-end on real hardware. See the community
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> for what has actually run live.

## The pattern

All the primitives below ship as of 0.1.0a8; see the [CLI reference](cli.md).

1. **Edit the boot entry over HID.** At the boot menu press `e` (GRUB) or
   `Tab` (syslinux/isolinux) — CLI `key`/`type`, or MCP
   `press_key`/`send_shortcut`/`type_text` — append the distro's args from the
   matrix below, then boot with `Ctrl+X` (GRUB) or `Enter`. This bootloader
   edit is the only part that must ride the KVM.
2. **Discover the installer's DHCP IP.** OCR it off the console —
   `kvm-pilot ssh-bootstrap` does exactly this — or read the DHCP server's
   leases. Verify with CLI `ssh-check` / MCP `ssh_reachable(host=…)` using the
   runtime `--ssh-host`/`ssh_host` override (#81): the installer's address is
   install-time DHCP, not the profile's configured host.
3. **Drive the rest over SSH.** CLI `ssh-exec` / the gated MCP `ssh_exec`, or
   an interactive session — a proper shell instead of keystroke-by-keystroke
   HID typing.

## Per-distro matrix

| Family | Mechanism | Kernel/boot args | Notes |
|---|---|---|---|
| **Fedora/RHEL/Rocky/Alma (Anaconda)** | `inst.sshd` + `inst.text` (optionally `inst.ks=<url>` for full kickstart) | append `inst.sshd inst.text` at GRUB edit | The documented remote-install path; text-mode Anaconda is fully keyboard/SSH-drivable. `inst.lang=en_US` avoids locale prompts. |
| **Debian / Ubuntu server legacy (debian-installer)** | `network-console` component | `anna/choose_modules=network-console netcfg/get_hostname=... network-console/password=...` (or preseed) | d-i pauses after network setup and offers an SSH login (`installer` user); resume the installer inside the SSH session. |
| **Modern Ubuntu server (Subiquity)** | autoinstall + live-session SSH | `autoinstall ds=nocloud-net;s=<url>` or interactive | The live installer runs sshd out of the box; `ssh` into the live session (password shown on tty1 / set via autoinstall `ssh` section) and drive or monitor from there. |
| **openSUSE/SLES (YaST/linuxrc)** | SSH install is first-class | `ssh=1 ssh.password=<pw>` (older: `sshpassword=`) | linuxrc starts sshd; run `yast.ssh` in the SSH session to launch the installer remotely. |
| **Arch / Alpine** | live ISO + manual sshd | none needed | Boot the live ISO, `passwd` + `systemctl start sshd` (Arch ships it enabled on the ISO since 2021), then the whole install is a shell script over SSH — no installer UI at all. |

## Fully automated installs

Every family also has a hands-off answer-file path — kickstart (`inst.ks=<url>`),
d-i preseed (`preseed/url=`), Subiquity autoinstall, AutoYaST — the GitOps route
when the same install must repeat. The automated GRUB-edit → discover-IP →
SSH-hand-off flow itself is the Reflexes `unattended-install` playbook's job
([#122](https://github.com/DustinTrap/kvm-pilot/issues/122)), not this page's.

## Missed the window? Retrofit with ssh-bootstrap

If you are already inside a graphical installer, `kvm-pilot ssh-bootstrap`
retrofits the SSH channel: it VT-switches to a text console, types a
marker-wrapped `echo` and OCRs the result (a console canary — if the marker
never echoes back, the keystrokes were not consumed by a shell and it aborts
before typing any sshd command), reads the DHCP IP, and starts `sshd`. It
**plans by default** — pass `--execute` to run it — and its default commands
deliberately do **not** set up auth: pass `--command` to install a key or
password, or the channel is reachable but unusable. See the
[CLI reference](cli.md) and
[`bootstrap.py`](../src/kvm_pilot/bootstrap.py) for the full model.

## Security notes

An installer's sshd is often unauthenticated or weakly authenticated:
Anaconda's `inst.sshd` gives **passwordless root**, and a
`network-console/password=` on the kernel command line is visible on-screen and
in boot logs. Use these mechanisms only on networks the user owns, set real
credentials immediately (an early `--command`/`ssh_exec` step), and remember
the installer-environment channel **dies at the installed system's first
reboot** — the installed OS needs its own sshd and credentials.

## Automation status

The detect-Linux-installer-and-prefer-text-mode automation — classify the boot
phase, edit GRUB, discover the IP, hand off — is deferred to the
[Reflexes](reflexes.md) `unattended-install` playbook
([#122](https://github.com/DustinTrap/kvm-pilot/issues/122), epic #117). All
required primitives (`send_shortcut`, `type_text`, `ssh-bootstrap`,
`ssh_reachable(host=…)`) exist as of 0.1.0a8; until the playbook lands, agents
follow this page by hand (the bundled skill carries the compact version).
