# Remote firmware update

`kvm-pilot` can **detect** stale KVM firmware (see [firmware-registry.md](firmware-registry.md))
and, on drivers that support it, **offer and perform a gated remote flash** of the
KVM's *own* firmware. This page documents the GL.iNet GLKVM update API, the
reliability model, and the operating procedure.

> **Read this first.** Flashing a KVM's own firmware is the single most destructive
> thing this tool does: the device reboots into a new image — dropping the very
> control channel you'd use to watch it — and on the GL RM1 family a failed or
> interrupted flash **bricks onboard storage** with **no remote recovery**. The
> command defaults to a dry-run and refuses to execute on a device with no
> out-of-band recovery path unless you explicitly override. Prefer flashing with
> physical (or same-site remote-hands) access.

## Known-good path: the vendor web console (#177)

**On the GL-RM1PE, the web console is the only known-good upgrade path.** A live
V1.5.1 release2 → V1.9.1 release1 upgrade was performed cleanly through the GL
web UI (the console stages the package, flashes, and auto-reboots in ~2 minutes).
The API path this page documents — `kvm-pilot firmware-update --execute` driving
`/api/upgrade/*` — was observed to **no-op on the same hardware** (`start`
returns 200, nothing flashes, #94/#95), and no API-driven flash has ever been
verified on any release. The driver verifies an observed upgrade-state change
and reports the no-op honestly (#94), and the `firmware-flash-webui-only` quirk
surfaces this guidance in `healthcheck` and `firmware-update` output. Until the
web console's endpoint sequence is reverse-engineered (#95), plan RM1PE upgrades
via the web UI, with physical/same-site access (no out-of-band recovery).

## Capability & commands

Remote update is the `FirmwareUpdate` capability (`drivers/base.py`), implemented
today only by `GLKVMDriver`. A driver advertises it structurally, so
`kvm-pilot capabilities` lists `firmware_update` when available.

- **`kvm-pilot healthcheck`** — when firmware is behind and the model's registry
  `profile.remote_update.supported` is true, the "Firmware update available" finding's
  remediation becomes actionable: it names the `firmware-update` command and states the
  risk. The healthcheck itself never flashes.
- **`kvm-pilot firmware-update`** — read-only by default: prints installed→latest, the
  reliability assessment, and the planned `/api/upgrade/*` steps, sending nothing.
  - `--execute` performs the flash (routed through the `firmware.flash` safety gate;
    without `--yes` it prompts first).
  - `--image PATH` uploads a local firmware image before flashing (preferred — it's
    deterministic and retryable from a known-good file); omit it to flash the image the
    device has already staged.
  - `--i-have-physical-access` overrides the refusal on a device with no out-of-band
    recovery path (acknowledges that a failed flash needs a physical trip).

```bash
kvm-pilot firmware-update --profile homelab2                       # assess only (dry run)
kvm-pilot firmware-update --profile homelab2 --image rm1pe.img --execute --yes
```

## The GL `/api/upgrade/*` surface

GL adds a proprietary upgrade layer on top of kvmd (**upstream PiKVM has no
OS-update REST API** — it updates via `pikvm-update` over SSH). GL publishes no
per-endpoint spec; the map below was reverse-engineered from live probing of a
GL-RM1PE (fw V1.5.1 release2, kvmd 4.82) plus the `gl-inet/glkvm` source, so treat
request bodies as **provisional** and feature-detect via `/api/upgrade/status`.

| Endpoint | Verb | Response / role |
|---|---|---|
| `/api/upgrade/status` | GET | `{"enabled": true}` — is the subsystem available |
| `/api/upgrade/version` | GET | `{"model","version"}` — installed version (also used by `get_firmware_info`) |
| `/api/upgrade/compare` | GET | GL's online check (installed vs GL's server); needs internet, and 502'd on the probed unit. Feeds `get_available_update`. |
| `/api/upgrade/download` | GET | `{"size": …}` — size of the available/staged image (~293 MB observed) |
| `/api/upgrade/upload` | POST | upload a local firmware image |
| `/api/upgrade/start` | POST | begin the flash; the device auto-reboots |
| `/api/upgrade/reboot` | GET | action-shaped status (`{"status":"Reboot started"}`) — **not** used by kvm-pilot |

`get_upgrade_status()` aggregates the three read endpoints. `apply_firmware_update()`
optionally uploads (`--image`) then POSTs `/api/upgrade/start`; it is routed through
`safety.guard("firmware.flash", …)` and defaults to `dry_run=True`.

## Reliability assessment

Per-model risk is **data** in the registry (`profile.remote_update`), not hard-coded:

```json
"remote_update": {
  "supported": true, "method": "gl-api", "risk": "high",
  "recovery_required": true, "self_flash_blind": true,
  "notes": "…"
}
```

For the GL RM1/RM1PE this is **`risk: high`**, because:

- **Recovery is physical-only.** A failed flash needs the GL **U-Boot failsafe**: hold
  Reset while powering on (blue LED flashes 5×), set your NIC to static `192.168.1.2/24`,
  browse to `http://192.168.1.1`, and upload the firmware. GL's docs state *"No remote
  recovery option exists."*
- **Self-flash is blind.** The KVM flashes itself and reboots, so kvm-pilot loses its
  only sensor/control channel across the reboot.
- **No A/B rollback**, and an interrupted power cycle bricks onboard storage (GL's own
  warning).
- **The update can disable the REST API.** A GL update commonly reverts
  `/etc/kvmd/nginx-kvmd.conf` and re-disables the PiKVM REST API kvm-pilot depends on,
  and may reset config — so even a *successful* flash can lock the tool out until the
  API is re-enabled over SSH (`gl-inet/glkvm#13`).
- **Media-mounted flashes corrupt.** A GL flash can start with virtual media still
  mounted, with no warning (`gl-inet/glkvm#120`) — `firmware-update` ejects media first.

### Operating procedure (before you `--execute`)

1. Run `kvm-pilot healthcheck` — fix a missing out-of-band recovery path if you can.
2. Ensure stable power (UPS). **Do not interrupt power during the flash.**
3. Eject virtual media (the command does this, but confirm the host isn't mid-install).
4. Prefer `--image` with a file from the vendor's stable channel over the online path.
5. Have the U-Boot recovery kit ready (laptop + Ethernet + the reset procedure above).
6. After the flash + reboot: if `/api/*` now 404s, re-enable the API in
   `/etc/kvmd/nginx-kvmd.conf`, restart kvmd, and **re-run `kvm-pilot healthcheck`**.

## Sources

- PiKVM OS update / recovery: <https://docs.pikvm.org/_update_os/>, <https://docs.pikvm.org/api/>
- GL firmware upgrade: <https://docs.gl-inet.com/kvm/en/faq/firmware_upgrade/>
- GL U-Boot debrick (recovery): <https://docs.gl-inet.com/kvm/en/faq/debrick/>
- GL firmware channel: <https://dl.gl-inet.com/kvm/rm1/stable>
- API re-disabled on update: <https://github.com/gl-inet/glkvm/issues/13>
- Flash starts with media mounted: <https://github.com/gl-inet/glkvm/issues/120>
- Update-induced hang (PiKVM): <https://github.com/pikvm/pikvm/issues/1539>

Tracking issue: [#92](https://github.com/DustinTrap/kvm-pilot/issues/92).
