# GLKVM onboarding — bringing a GL.iNet Comet online

> **Operator runbook** for the GL.iNet GLKVM fork (GL-RM1 "Comet", GL-RM1PE
> "Comet PE"). The build/protocol reference is
> [`driver-features.md`](driver-features.md#glkvm--glinet-fork-gl-rm1-comet--gl-rm1pe-comet-pe);
> this is *what to expect and the ordered steps to get working*, seeded from the
> real failures three of these units produced during live bring-up.

GLKVM is kvm-pilot's **primary live-tested target** — most of the honesty rules
in this project exist because a GL unit reported something untrue.

---

## Expectations — read this first

Four things surprise every newcomer, and all four are the device, not the tool:

1. **The REST API ships disabled.** Out of the box every `/api/*` returns 404.
   Nothing works until you enable it, and a firmware upgrade can silently revert
   it.
2. **The video encoder is on-demand.** It runs only while a video client is
   connected. On an idle unit `snapshot` would 503 forever — kvm-pilot works
   around this by connecting a WebSocket stream client first (~1.5 s), but it is
   why the first snapshot is slower than the rest.
3. **Power readings are not trustworthy.** ATX reports `enabled=false` and
   "off" *while the host is plainly running*. This is the project's canonical
   **false-report** case: never confirm a power action from the ATX read — look
   at the screen.
4. **It is blind below the OS on a laptop.** HDMI capture only sees what the
   host routes to that output; a laptop routes firmware video to its internal
   panel. BIOS, POST and GRUB will not appear. See
   [pre-boot video](driver-features.md#pre-boot-video-video-does-not-mean-you-can-see-bios-210)
   — on a laptop you want `amt`, not this.

## Prerequisites

- The appliance's IP (the **KVM's** address — not the managed host's).
- Web-console credentials (`admin` by default — change it).
- `pip install --pre kvm-pilot`.
- Optional: appliance-SSH (`root@<kvm-ip>`, key-based) for the diagnostics REST
  cannot see — see *Hazards* below.

## 1 — Enable the REST API (once per unit, and after every firmware upgrade)

SSH to the appliance (or use the web console's terminal) and uncomment the
kvmd block in:

```
/etc/kvmd/nginx-kvmd.conf
```

then restart kvmd. Until you do, kvm-pilot reports `ApiDisabledError` and names
this file — that error is the tool telling you it got a 404 on *every* endpoint,
not that the device is unreachable.

## 2 — Write a profile

```toml
[hosts.mykvm]
host = "10.0.0.11"
driver = "glkvm"
user = "admin"
# passwd via KVM_PILOT_PASSWD, not here
verify_ssl = false        # GL ships a self-signed certificate
```

`driver` is optional — leaving it out makes kvm-pilot probe the device and
identify the GL fork from its proprietary `/api/upgrade/version` endpoint
(#235). Pinning it skips the probe.

## 3 — Run the intake gate

```bash
kvm-pilot healthcheck --profile mykvm
```

**This is the gate, not a formality.** Expect on a healthy GL-RM1PE:

| Result | Meaning |
|---|---|
| `api-reachable` OK | step 1 worked |
| `recovery-path` **CRITICAL** | normal unless ATX is physically wired — see below |
| `preboot-video` OK (`capture`) | expected; the laptop caveat above |
| `video-signal` INFO "streamer idle" | expected on an idle unit, not a fault |
| `firmware-quirks` WARNING | the four quirks below apply |

The `recovery-path` CRITICAL is the one to take seriously: it means **no
out-of-band reset exists**. If the target hangs, you have no remote way to
power-cycle it. Wire the ATX cable to the host's front-panel header, or accept
that recovery needs hands.

## 4 — Verify you can see and type

```bash
kvm-pilot snapshot --profile mykvm screen.jpg   # first one is slow (encoder wake)
kvm-pilot capabilities --profile mykvm          # also prints the video scope
```

## What you can do now

Power (if ATX is wired), HID keyboard/mouse, snapshots + vision classification,
virtual media (ISO mount), GPIO, event streaming, device logs, and — via
appliance-SSH — encoder diagnostics and a gated appliance reboot.

## Hazards & troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every `/api/*` returns 404 | REST API disabled (ships that way; upgrades revert it) | Step 1. Re-check after any firmware upgrade |
| `snapshot` 503s forever on an idle unit | On-demand encoder, no video client | kvm-pilot wakes it automatically; if it persists the encoder is wedged — see below |
| Snapshot returns an undecodable frame | H.264 at native resolution — a lone NAL mislabeled `image/jpeg` | Lower the capture resolution (EDID) to 1024×768, or upgrade to V1.9.1 where JPEG works |
| Power says "off" while the host is running | **ATX false-report** — the canonical one | Never trust it. Confirm power visually via `snapshot` |
| `power` returns HTTP 500 | ATX not wired to the host header | Wire it, or use Wake-on-LAN / a BMC path |
| Load average ~10 on an idle appliance | RV1126 video threads park in D-state | **Not a health signal.** `kvm-pilot appliance loadavg` says so in its own output |
| Encoder wedged (503 + D-state threads) | RV1126 hardware pipeline stuck above 1080p (#107) | `kvm-pilot appliance reboot` — the only fix; REST cannot see or clear it |
| HID stops reaching the target | USB gadget de-enumerated | `kvm-pilot recover-hid`; if it fails the cable is charge-only or in a non-host port |
| Remote firmware flash reports success, nothing happens | GL's `/api/upgrade/start` is a **no-op** on real RM1PE (#94/#95) | Flash from the GL web console. kvm-pilot detects the no-op and says so |
| Target went dark mid-session | Guest idle-suspend | Wake-on-LAN first (`kvm-pilot wake`), then disable idle-suspend on the host |

## Security posture

- **TLS is self-signed**, so `verify_ssl` defaults false — credentials cross an
  unauthenticated channel. Pin the device certificate with `ssl_ca_file` on any
  network you do not fully control, and never expose the unit to the internet.
- **Change the default password.** `healthcheck`'s `default-creds` check fails
  loudly if you have not; repeated default-credential logins can lock the account.
- **Appliance-SSH is a second trust domain** — `root` on the KVM itself, distinct
  from the kvmd REST credentials. It is opt-in and key-only on purpose.
- Keep the config file `chmod 600` if it holds a password; kvm-pilot warns when
  it is group/other-readable.

## See also

- [`driver-features.md`](driver-features.md) — per-capability reliability and testing level
- [`troubleshooting.md`](troubleshooting.md) · [`configuration.md`](configuration.md)
- [`test-plan.md`](test-plan.md) — the fleet-sweep procedure these hazards came from
