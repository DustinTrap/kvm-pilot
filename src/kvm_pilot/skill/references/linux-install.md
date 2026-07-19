# Installing Linux? Switch to text mode + SSH first

> Part of the bundled kvm-pilot skill. Read this before starting any OS install
> through the KVM. Also served at runtime by the MCP `doctrine` tool (topic
> "linux-install").

When the task is a Linux install through the KVM, do **not** click through the
graphical installer (coordinates are unreliable, #128/#129). Before the
installer boots, edit the boot entry over HID — `e` at GRUB / `Tab` at syslinux
(`press_key`/`send_shortcut` + `type_text`) — append the distro's text+SSH args,
boot (`Ctrl+X`/`Enter`), and finish over SSH:

| Family | Append / do |
|---|---|
| Fedora/RHEL/Rocky/Alma | `inst.sshd inst.text` (+`inst.lang=en_US`; `inst.ks=<url>` for fully automatic) |
| Debian / Ubuntu-legacy d-i | `anna/choose_modules=network-console network-console/password=<pw>` — SSH in as `installer` |
| Ubuntu Server (Subiquity) | live sshd already running; `autoinstall ds=nocloud-net;s=<url>` for hands-off |
| openSUSE/SLES | `ssh=1 ssh.password=<pw>`, then run `yast.ssh` in the session |
| Arch / Alpine | none — live-ISO shell: `passwd` + start `sshd` |

After boot: discover the DHCP IP (`ssh-bootstrap` OCRs it off the console; or
DHCP leases), verify `ssh_reachable(host=…)`, then drive via `ssh_exec`. Already
stuck in a GUI installer? `kvm-pilot ssh-bootstrap` retrofits the channel (see
[interfaces.md](interfaces.md)). Caution: installer sshd is weakly authenticated (Anaconda
`inst.sshd` = passwordless root) — LAN-you-own only, set credentials immediately.
Full matrix + rationale:
[docs/unattended-install.md](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/unattended-install.md).
