# GLKVM (GL.iNet RM1PE) — field notes: right-way workflows, quirks & features

> **Draft.** Session-derived operational notes for the GL.iNet GLKVM fork (first target: GL-RM1PE, firmware V1.9.1, kvmd 4.82). Promote to a formal per-driver doc, or fold into a per-KVM-type *skill*. Cross-refs: #77, #107, #141–#144, #149.

The theme: **drive everything through the kvm-pilot API, not the device's back door.** Root-SSH-ing the appliance and poking `kvmd-otgmsd`/`scp` *seems* faster but fights kvmd's own state and hits GL-specific gaps (no `sftp-server`, undocumented media paths). The API primitives (`mount_iso`, `healthcheck`, `snapshot`, HID, `power`, the in-band `ssh` channel to the *target*) already encode the right behavior.

## 1. Approval posture (do this first)
On a chat client, per-action MCP **elicitation** approvals get cancelled by the next chat message — HID/media/power calls return `approved:false` + `denied_reason:"approval cancel"` **silently** while read-only calls keep working, so it looks like the host is ignoring input. Set **`KVM_PILOT_MCP_ELICIT=off`** (env gate + `confirm=true` become the standing authorization) and reconnect. Signature to watch: `approved:false` + `approver:null`. (#149)

## 2. Video / snapshot (the biggest trap)
- The streamer is **on-demand** (only runs while a viewer/WebRTC client is connected) and the JPEG `snapshot` path **503s or returns a frozen/undecodable frame** at non-standard modes — the LT86102 HDMI chip logs `get resolution failed, waiting for HDMI signal` → `Stream is offline`. Seen at the 1024×800 desktop, at GRUB, and during OS-install mode transitions. (#107, #141–#144)
- **WebRTC (the web UI) keeps showing video even when JPEG snapshot fails.** So: when snapshot goes blind, **the human on the web UI is the reliable set of eyes** — don't assume a frozen/black frame is ground truth (check the `frame_ref` hash; identical hash across a real screen change = stale).
- Practical: snapshots are reliable at standard ≤1080p modes (BIOS/POST/Windows Setup usually fine). Expect blind spots during boot mode-switches; keep a human watching for those windows.

## 3. Power / recovery
- On a **laptop** target there's no ATX/GPIO wiring → **no out-of-band reset** (healthcheck flags this CRITICAL), and **power/LED readings are not trustworthy**. Verify state visually; keep someone on-site for a hang. Reboot the *target OS* in-band (`ssh … systemctl reboot`) rather than blind power tricks.

## 4. Virtual media — the right way
- Use **`mount_iso`** (API): it uploads to the device's media store (`/userdata/media`, ~27 GB on RM1PE) **and** attaches via kvmd, handling GL's `kvmd-media` proxy. `usb=false` (CD-ROM) for OS ISOs.
- **Verify it's really online** with `healthcheck` → `msd-online: "Image attached and online (presented to host)"`, guarding against the GL **connected-but-offline** quirk (#77). A strong extra check: if the target OS is up, it should now see the disc (`lsblk` → an `sr0` of the ISO's size).
- **Don't** `scp`/`kvmd-otgmsd` by hand: the device has **no `sftp-server`** (plain `scp` fails with "Connection closed"), and the low-level hand tool can desync kvmd's view.

## 5. Booting a target to the virtual media
- **Best (no F9 timing):** if you have in-band OS access, set a one-time UEFI boot from the target: `efibootmgr -n <USB-media BootNum>` (the KVM media shows up as e.g. `USB Drive (UEFI)`), then reboot. Misses fall back safely to the normal OS entry (recoverable — you keep SSH, retry).
- **Else:** on-site **F9** boot menu.
- Watch for **"Press any key to boot from CD or DVD…"** on CD-ROM media — a ~5 s window; if snapshot is blind here, have the human tap a key.

## 6. HID
- Mouse is **absolute** (good for clicking Setup/BIOS). During target USB re-enumeration (reboots) the log shows `HID-keyboard is busy/unplugged (write select)` flapping — transient; don't over-interpret it as a target problem.

## 7. The in-band SSH channel is your friend
When video flakes, the kvm-pilot **ssh channel to the *target OS*** (not the appliance) is the reliable path: hardware inventory, `efibootmgr` BootNext, clean reboots, driver verification. Use it whenever the guest OS is reachable; fall back to blind KVM driving only when it isn't.
