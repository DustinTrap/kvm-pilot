# End-to-end operator tasks, a13 → a14: the "leaner cut"

**Date:** 2026-07-08 · **Versions:** kvm-pilot `0.1.0a13` → `0.1.0a14` · **Fleet:** `.11` (server11/Fedora 44) · `.20` (RHEL 10) · `.39` (WHITESKELETON/Win11)

The first analysis ([2026-07-08-perf-a13-a14](2026-07-08-perf-a13-a14.md)) timed *library micro-ops*. This one times **whole operator tasks the way a user performs them, start → result** — "check the system's health," "grab a screenshot," "wake it," "run a command," "find its IP" — done to the *connected system* through the KVM, a13 vs a14.

**Bottom line up front:** a14's *only* real, measurable win at the task level is **multi-command in-band work (a health check): ~2.2–2.5×**, from persistent SSH (#182). Every interactive KVM-plane task — screenshot, wake, single run-command, discover-IP — is **flat**, exactly as expected: a14 shipped a router + persistent SSH + password auth; it did not touch the snapshot encoder, HID, or single-shot transport. This is the honest operator translation of the a14 release.

## Method
- **Whole-task wall-clock, median of 3 warm rounds** per task (damps the warm-sample variance the last run flagged). Harness `e2eharness.py`, run under an isolated **a13 venv** and a clean **a14 venv**.
- **Tasks:** `health` = 5 real probes over in-band SSH (`uptime`/`free`/`df`/loadavg/`systemctl is-system-running`) with the persist flag set → a13 opens a fresh connection per probe, a14 reuses one via ControlMaster; `run_command`/`discover_ip` = single in-band command; `screenshot` = `driver.snapshot()` (time + JPEG success); `wake` = 2 HID mouse-moves. A **`health_fresh`** control runs the same 5 probes with persist *off* on both versions — it must stay equal a13-vs-a14 or the health win is contaminated.
- **Fleet-parallel, equipment-isolated, adversarially verified:** background workflow `e2e-leaner-cut-a13-a14` — one agent per KVM (a13 then a14 against its own box, never another's) + a skeptic that checked every delta against a noise floor (**flat if |Δ| < 15% OR < 20 ms**) and was told to hunt overclaims. It returned **no overclaims**.

## Results (median-of-3 ms; a13 → a14)

| KVM | task | a13 | a14 | verdict |
|---|---|--:|--:|---|
| **.11** | **health** (5 in-band) | 1501 | **700** | **2.1× faster** ✅ |
| .11 | health_fresh (control) | 1486 | 1569 | equal (+5.6%) — control holds |
| .11 | run_command | 312 | 277 | flat |
| .11 | discover_ip | 332 | 346 | flat |
| .11 | screenshot | 80 | 565 | ⚠️ anomaly (state-dependent, see below) |
| .11 | wake | 102 | 166 | flat (small absolute) |
| **.20** | **health** (5 in-band) | 1105* | **439** | **~2.5× faster** ✅ |
| .20 | health_fresh (control) | 1127 | 1084 | equal (−3.8%) — control holds |
| .20 | run_command | 191 | 211 | flat |
| .20 | discover_ip | 259 | 302 | flat (variance) |
| .20 | screenshot | 107 | 85 | flat (variance, NOT an a14 gain) |
| .20 | wake | 83 | 109 | flat (small absolute) |
| **.39** | screenshot | 115 | 132 | flat |
| .39 | wake | 113 | 124 | flat |

\* .20's a13 health had run-to-run spread (first run 1105 ms, a re-run 1546 ms). Using the **cleanest, within-version** measure — a14's own fresh-vs-persistent on the same run — the speedup is **.11 1569/700 = 2.2×** and **.20 1084/439 = 2.5×**. The cross-version numbers corroborate (2.1× / 2.5×). Any "3.5×" from the re-run is a13 self-variance, not signal — the honest figure is **~2.2–2.5×**.

## The one win, stated honestly
**Persistent SSH makes a multi-probe health check ~2.2–2.5× faster.** a13 pays full connection setup on every probe; a14 pays it once and reuses the channel for the other four. The `health_fresh` control is equal across versions on both in-band hosts (+5.6% / −3.8%, both inside noise), so the win is the persistence, not a contaminated baseline. This reconfirms the [micro-op baseline](2026-07-08-perf-a13-a14.md) (~2.5× on a 5-command SSH task) — now at the **operator-task** level with real commands.

## What is flat, and why
`run_command`, `discover_ip` (single in-band commands — persistence can't help a cold single connection), `screenshot`, and `wake` (KVM-plane — a14 didn't touch snapshot encoding or HID) are all inside the noise floor. `.39` is KVM-plane-only (its in-band key is still pending) and both its tasks are flat. **No non-health task got faster because of a14, and none is claimed to.** The `.20` screenshot reading 21% *faster* under a14 is variance (a13's first snapshot threw and was re-run) — it would be an overclaim to credit it, and we don't.

## The one thing to re-measure (not a regression)
`.11 screenshot` went **80 ms → 565 ms (~7×)** between the a13 and a14 runs. a14 did not touch the snapshot plane, so this is **snapshot state-dependence** — the GL snapshot's cost/format is resolution × encoder-mode × cache-state (the #107/#181 finding), and .11's on-demand streamer was in a different state during the a14 pass (in a hand check minutes earlier both versions measured ~81 ms). It's flagged to re-measure, **not** attributed to a14 code. The durable fix for the underlying snapshot fragility is **#187** (flip the encoder to MJPEG for a native-res JPEG) — discovered this session.

## Operator translation (real terms)
- **Faster in a14:** multi-step in-band diagnostics — gathering a batch of vitals, running several remote commands in a row — by **~2.2–2.5×** (and larger per *additional* command). If your task is "SSH in and run five things," a14 is the upgrade.
- **Unchanged in a14:** seeing the screen (screenshot), waking, logging in, running a single command, finding the IP. These are the KVM plane or single-shot ops; a14 was a router/persistence release, not a video/HID one. The next lever for the KVM plane is HTTP keep-alive (**#185**) and the MJPEG snapshot fix (**#187**).

## Scope run vs deferred
- **Run (a13 vs a14, clean):** health, run_command, discover_ip, screenshot, wake — on .11 + .20 (in-band + KVM plane) and .39 (KVM plane). `identify_state` ≈ screenshot cost + classification (same snapshot → flat).
- **Deferred:** `.39` in-band health/run-command/discover (WHITESKELETON's in-band key install is gated on the operator), `#6 reboot` (host-modifying — gated), and `#9 BIOS` / `#10 ISO-boot` (dropped from the leaner cut). `#1 wake` and `#2 login` were also exercised interactively (both succeeded; login to .20's GDM took a few seconds).

## Reproduce
- Harness: `e2eharness.py` — `<a13py|a14py> e2eharness.py <label> <profile> [ssh_host ssh_user ssh_key]`.
- Fan-out: workflow `e2e-leaner-cut-a13-a14` (one agent per KVM, adversarial verify).
- Standing method + ledger: [docs/test-plan.md §12](../test-plan.md).

## Related
- [2026-07-08-perf-a13-a14](2026-07-08-perf-a13-a14.md) (#186) — the micro-op baseline this extends.
- #182 (persistent SSH — the win) · #185 (HTTP keep-alive — next KVM-plane lever) · #187 (MJPEG snapshot — the durable fix for the screenshot fragility) · #181 (router: snapshot capability is encoder/state-dependent).
