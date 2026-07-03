# Firmware registry: currency, capability profile & ingestion (#80 follow-up)

## The finding

The device preflight healthcheck (#80) has a **firmware currency** pillar, but it
was under-built: it reported the *running* version as `INFO` and listed known
quirks, yet **never checked whether the firmware was current or known-bad**, and
carried nothing about a device's *capabilities / expected experience*. #80 asked
the pillar to "flag known-bad or EOL firmware **and available updates**." A device
on outdated firmware (often the cause of the other findings) sailed through.

## One generic mechanism for every family

A device is identified by the `{vendor, product, version}` its driver's
`get_firmware_info()` normalizes. The *only* vendor-specific knowledge — how to
read the version off the box — lives in the driver:

| driver | vendor | product | version (the comparable one) |
|---|---|---|---|
| `glkvm` | `gl.inet` | reported board (`Rockchip RV1126B-P …`) | kvmd version (`4.82`) |
| `pikvm` / `blikvm` | `pikvm` / `blikvm` | reported board | kvmd version |
| `redfish` | `Manufacturer` (Dell/HPE/…) | Manager `Model` (`iDRAC 9`, `iLO 5`) | Manager `FirmwareVersion` |

The registry and the checks stay 100% generic. Adding IPMI or AMT later is just a
new driver returning the same three fields — **no change to the registry or the
checks**. (On the PiKVM family a device can't report its *product* firmware
version, so kvmd is the currency proxy; on BMCs the reported version *is* the
upgradeable one.)

## Data model (`data/firmware_registry.json`, schema v2)

```jsonc
{
  "schema_version": 2,
  "updated": "YYYY-MM-DD",
  "firmware": [
    {
      "vendor": "dell",                  // == get_firmware_info().vendor (case-insensitive)
      "product": "iDRAC9",               // substring of get_firmware_info().product
      "latest": "7.10.30.00",            // latest known release (device's own scheme)
      "source": "https://…", "date": "2026-05-01",
      "known_bad": [
        {"affected": "<=6.10.30.00", "severity": "critical",
         "issue": "…", "fixed_in": "6.10.80.00", "source": "https://…"}
      ],
      "profile": {                       // capability / expected-UX (all optional, incrementally enriched)
        "mouse": "absolute",            // absolute | relative | none  → GUI usability
        "vmedia": "reports-only",       // reliable | reports-only | none → boot-from-ISO fidelity (#77)
        "power_state_trusted": false,   // are ATX/LED readings truthful → safe to automate reboots?
        "video": "h264/1080p60"         // rated transport + ceiling (fallback when unreadable live)
      }
    }
  ]
}
```

### Why these `profile` fields (and only these)

The report a user actually wants — *what can this do and how good is the
experience* — has ~5 axes, but most are **detectable live** by the healthcheck
(video codec/res/fps from `/api/streamer`, recovery path, exposed services). We
store **only the differentiators a live probe can't safely determine**:

- **mouse** — absolute vs relative decides whether a GUI is usable; can't probe
  without moving the pointer on a live host.
- **vmedia** — `reports-only` is the #77 trap (API says mounted, host sees an
  empty drive); can't probe without a reboot.
- **power_state_trusted** — whether power/LED readings can be believed (the GL
  quirk behind the `.18` no-recovery incident); a stored fact, not observable.
- **video** — a fallback ceiling for devices whose stream API won't report it.

Everything else on the report is computed live and combined. `get_firmware_info()`
can auto-populate the observable half, so humans only supply these few facts —
and can enrich them over time (an initial profile on first contact, a follow-up
report adding `vmedia` once an ISO has actually been booted).

## The checks (`health.py`, generic — no per-vendor branching)

- `check_firmware_currency` — match `(vendor, product)`; then:
  1. installed version satisfies a `known_bad.affected` range → that severity;
  2. else `version < latest` (ordered `_vercmp`) → `WARNING` "update available";
  3. else **nothing** — a current device stays quiet (no over-reporting).
- `check_capability_profile` — surfaces the stored profile as the expected-UX
  line: `INFO` when all-good, `WARNING` when any axis is degraded (relative/no
  mouse, non-`reliable` vmedia, untrusted power).

`_vercmp` compares each dot/dash segment numerically (correct for `4.82`,
`7.10.30.00`, `2.78`); genuinely non-numeric schemes only ever compare equal and
fall through to exact `known_bad` matches rather than a bogus ordering. Version
strings in the registry must use the device's own scheme (write `4.90`, not `4.9`,
if you mean build 90).

## Distribution & refresh

- **Bundled on PyPI.** `data/firmware_registry.json` ships in the wheel, so the
  check works fully **offline** — the stdlib-only core never fetches on its own.
- **Loader precedence** (`load_registry`): `KVM_PILOT_FIRMWARE_DB` (explicit
  file) → the user cache a refresh writes (`~/.cache/kvm-pilot/`) → the bundled
  copy. A missing or invalid override is skipped, so a bad refresh can never take
  the check offline.
- **Opt-in refresh** (not the default, never automatic): pull the latest registry
  from `DEFAULT_DB_URL` (the repo's raw file on GitHub's CDN — the single source
  of truth), validate it, and write it to the user cache. Explicit only —
  air-gapped / mgmt-LAN use and privacy mean we don't phone home per run.

## Ingestion — fully automated on GitHub (free on public repos)

1. **Issue Form** (`.github/ISSUE_TEMPLATE/firmware-report.yml`) — a contributor
   picks a submission type (latest release / known-bad / capability profile) and
   fills structured fields.
2. **Action** (`.github/workflows/firmware-ingest.yml`, gated on the
   `firmware-report` label) runs `python -m kvm_pilot.firmware_registry`, which
   parses the issue body **as data** (never eval'd), validates it, merges it, and
   the workflow **opens a PR automatically** via `GITHUB_TOKEN`. Invalid
   submissions comment the errors back on the issue; duplicates are a no-op.
3. Profiles merge **field by field**, so partial/enriching reports accumulate.

GitHub-hosted runners are free and unlimited for public repos; each run is
seconds. The issue body is untrusted input, so a spam/injection issue just fails
validation harmlessly.

## Seed data

The registry ships empty; real `(vendor, product)` entries are added through the
ingestion pipeline (no fabricated firmware facts — see CLAUDE.md). The mechanism
is verified end-to-end against a live GLKVM.
