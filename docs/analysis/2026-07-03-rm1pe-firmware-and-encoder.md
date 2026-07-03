# 2026-07-03 — RM1PE: remote firmware update declined; native-res encoder wedge

*Session-level analysis (see `docs/analysis/`): a field report from a real GL
**RM1PE** (homelab2, 10.0.1.20, firmware V1.5.1 release2). Two operator-facing
conclusions for anyone running this hardware — **do not remote-flash an RM1PE
from kvm-pilot yet**, and **keep RM1PE guests at ≤1080p** or you lose stills and
pin the box at load ~10.*

## The ask

Run kvm-pilot against 10.0.1.20 for a status report; restore snapshot "vision";
and, *if it can be done safely*, perform the remote firmware upgrade
(V1.5.1 release2 → the registry's latest, V1.9.1 release1) — then publish a
report so other operators benefit.

## Firmware update — assessed, declined

The upgrade was **not** performed. Three independent blockers, each sufficient on
its own:

1. **It does not work on this model (a proven no-op).** `POST /api/upgrade/start`
   returns success but flashes nothing on RM1PE — version unchanged, no reboot,
   LEDs solid — and the request bodies are provisional / not vendor-documented.
   This is already recorded in **#94** (false success) and **#95** (start is a
   no-op); both are open, and the driver path (`drivers/pikvm.py`) is unchanged
   since. Best case, attempting it repeats the misleading "flash started"; worst
   case, a partial flash.
2. **It fails kvm-pilot's own safety gate.** `healthcheck` reports a **CRITICAL
   `recovery-path`** finding on this unit: ATX `enabled=false`, no GPIO power
   channels — *no out-of-band reset*. A failed flash needs physical access to
   recover, which is exactly the condition the destructive-op gate exists to
   block.
3. **The device was already degraded** at assessment time (encoder wedged, load
   ~10 — see below): the worst moment to flash.

The supported route for an RM1PE that genuinely needs updating remains the vendor
UI — <https://dl.gl-inet.com/kvm/rm1/stable> — which requires someone at the
device, not a kvm-pilot remote op. "If it can be done safely" was not satisfiable.

## Restoring vision — what actually blocks it

The status report showed `snapshot` returning an undecodable image and
`classify_screen` erroring. Diagnosis (full evidence in **#107**):

- **`classify_screen`** failed only because the kvm-pilot process had no
  `ANTHROPIC_API_KEY` — environmental, not the device.
- **`snapshot` returns H.264, not JPEG.** `GET /api/streamer/snapshot` answers
  `200 image/jpeg`, but the body is a **78-byte H.264 NAL** (`00 00 00 01 41 …`).
  The streamer runs `--venc-format=0` (H.264-only) and the GL endpoint hands back
  a coded frame instead of a JPEG. The browser KVM looks fine (WebRTC consumes
  H.264 directly); kvm-pilot's still-image tools cannot.
- **The RV1126 encoder wedges at native resolution.** With the guest at
  2560×1440, ten Rockchip media threads (`venc vpss vvi_thread vrga_0 …`) sit in
  uninterruptible **D-state**, pinning load at ~10. A reboot clears it for ~60s,
  then it **re-wedges** because the input is still 1440p. The `snapshot` JPEG path
  worked earlier the same day at 1080p (ledger `real-rm1pe-20260703`, "jpeg
  1080p") — so the failure is **resolution-dependent**, not a one-off hang.

### Reboot and EDID: what we tried

- **Appliance reboot** (over SSH — kvm-pilot has no appliance-reboot path, and the
  guest ATX reset is disabled): cleared the D-state threads and dropped load to
  ~0.1 for ~60s, then the pipeline re-wedged on the unchanged 1440p input. A
  reboot alone is not a fix here.
- **EDID cap** (maintainer edited the advertised EDID by hand): the guest
  renegotiated down to **1920×1200**, but the encoder still wedged
  (`no support format=a,[1920,1280]`, load back to ~10) and `snapshot` still
  returned the 78-byte NAL. **1200p is not low enough** — a true **1920×1080** is
  the next thing to try.

## Takeaways for RM1PE operators

- **Don't remote-flash from kvm-pilot** on RM1PE until #94/#95 are validated on
  real hardware; use the vendor UI, and only with a wired ATX/GPIO recovery path.
- **Keep the guest at ≤1080p** for reliable stills. Above 1080p, `snapshot` yields
  H.264 (not JPEG) *and* the encoder wedges at load ~10 — while `healthcheck`
  still cheerfully reports "capture stream is live." Capping the KVM EDID works
  only if it caps to 1080p (1200p still wedges).
- **`power_state` is not trustworthy** on RM1PE (ATX always reads off); verify
  power visually, never automate a blind reboot.

## Follow-up: manual upgrade to V1.9.1 release1 (same day)

The maintainer flashed the unit **manually via the vendor UI** to **V1.9.1
release1** (kernel 6.1.118 → 6.1.141; adds Pion/WebRTC extras). Re-running against
the upgraded box gives a **split result**:

- **`snapshot` is fixed by the firmware.** `GET /api/streamer/snapshot` now returns
  a real **43 KB JPEG** (`Snapshot successful: JPEG size=43262 bytes (from cache)`),
  reliably (6/6), at 1920×1200 — the V1.5.1 "78-byte H.264 NAL" behaviour is gone.
  V1.9.1 serves a **cached JPEG decoupled from the H.264 encoder**, so a still no
  longer depends on the encoder's health. Snapshot/vision is restored. *(One
  transient H.264 response was seen in the first seconds after boot, during
  streamer warm-up; it settled to JPEG immediately after.)*
- **The RV1126 encoder wedge is NOT fixed.** At 1920×1200 the same ten media
  threads (`venc vpss vvi_thread …`) are still in D-state with load ~10 on V1.9.1.
  So the wedge is not native-res-only — 1200p wedges too — and firmware didn't
  address it. A true **1920×1080** is still the recommendation. The saving grace is
  that on V1.9.1 the wedge no longer breaks stills.
- **The upgrade validates the *vendor* path, not kvm-pilot's.** This was a manual
  vendor-UI flash. kvm-pilot's own remote-flash path (#94/#95) remains a no-op /
  unverified — the successful upgrade says nothing about it.
- Firmware currency clears: the `firmware-currency` WARNING is gone; the box is on
  the registry's latest. `recovery-path` is still CRITICAL (hardware wiring, not
  firmware) and TLS is still unverified.

Net for #107: the **snapshot-format** half is resolved by V1.9.1; the
**encoder-wedge** half stands.

## What changed as a result

- New GitHub issue **#107** (snapshot-H.264 / encoder-wedge above 1080p), linked
  to #94/#95 and the support-matrix epic.
- New run-ledger entry `real-rm1pe-20260703b` in `data/test_runs.jsonl` recording
  `snapshot` = fail at native res, which surfaces on the community
  Hardware-Compatibility page as the resolution-dependent (⚠️ mixed) verdict it is.
- This narrative, mirrored into the wiki via `build_wiki.py`.

*Note: `CLAUDE.md` still says the project has "never run on real hardware." As of
2026-07-03 that is no longer true for the GL glkvm driver — the read-only and
snapshot paths have now been exercised on an RM1PE; the flash path has been shown
to be a no-op (#94/#95). Worth reconciling that line in a follow-up.*
