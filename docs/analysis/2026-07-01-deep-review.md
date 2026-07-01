# 2026-07-01 — Top-to-bottom review: narrative

*This is the first entry in `docs/analysis/` — session-level analysis output
that is too narrative for `decisions.md` (which records individual judgment
calls) but worth keeping: what was reviewed, how, what was found, and what
changed as a result.*

## The ask

A full product-manager + technical-SME review of the codebase and every open
issue: file researched issues for findings, fix what could be fixed safely in
one session, and interview the maintainer where the answer had to come from
him rather than the code.

## The interview (product direction)

Four questions were put to the maintainer up front; the answers reshaped the
whole session's priorities:

| Question | Decision |
|---|---|
| Hardware for validation? | **GL-RM1PE on hand / arriving soon** — hardware validation is the critical path; emulators are a bridge, not a substitute. |
| Primary audience? | **AI-agent builders (MCP/Claude)** — the MCP server + skill + safety story is the flagship surface; the CLI is secondary. |
| Release posture? | **Cut an installable `0.1.0a2` soon** — keep alpha warnings loud, but let `pip install --pre` work so community validation can start. |
| Filing & fixing? | **One issue per finding; small verified fixes committed straight to `main`** and cross-referenced. |

Two follow-up decisions were made after the findings landed: **[#38][i38]
accepted** (keep unverified TLS as the default, but warn once and add
`ssl_ca_file` pinning — shipped the same day) and **[#56][i56] deferred**
(branch protection changes the direct-push workflow; revisit later).

## How the review ran

The review was a structured multi-agent pass rather than one long read:

1. **Nine parallel domain reviewers**, each with its own lens and file set:
   PiKVM core, Redfish (spec fidelity against DSP0266/DSP0268), vision, CLI +
   config + packaging, MCP server + skill, security, tests + CI/CD, docs
   accuracy (every claim treated as a hypothesis to verify against the code),
   and product/backlog health.
2. **Adversarial verification**: every finding went to an independent skeptic
   agent instructed to *refute* it — re-reading the code, re-running the
   reasoning, checking the specs, often reproducing the failure empirically
   (e.g. a stall server proving read-timeouts escaped as raw
   `builtins.TimeoutError`; a coverage run confirming the exact untested
   lines). Findings the skeptic could refute were dropped.
3. **A completeness critic** then swept for blind spots — subsystems no
   finding touched, cross-cutting concerns no single domain owned.

In total: **102 agents, ~1,360 tool calls**. Of 92 candidate findings, **86
survived verification** (16 high, 40 medium, 30 low) and 6 were refuted — the
refuted ones are exactly why the verification pass exists.

## What was found (the shape of it)

The codebase reviewed well for an alpha: spec-literate Redfish driver, honest
docs, real security intent (redaction, same-origin guards, Trusted
Publishing). The serious findings clustered where **mock-only testing cannot
see**:

- **The safety layer had decorative stretches.** HID keystrokes bypassed it
  entirely (`type "rm -rf /"` sailed past `--dry-run`), `mount --dry-run`
  really uploaded the ISO before any gate fired, and dry-run prompted for
  confirmation it would never use — blocking unattended automation.
- **Transport failure modes were untested and wrong.** Read-phase timeouts and
  resets escaped both HTTP transports as raw builtins (unmapped, tracebacking
  through the CLI), and the retry layer could re-fire a destructive POST whose
  response was lost — the double-power-cycle case.
- **Real-BMC lifecycle gaps in the Redfish driver**: sessions leaked (no
  `close()` caller), no 401 re-login, and `PushPowerButton` chosen as a
  power-off fallback can power an already-off host *on* (intent inversion,
  verified against iDRAC8 `AllowableValues`).
- **The vision gates could lie.** An ATX-less device (no add-on board) made
  `is_powered_on()` read a meaningless LED as "off", short-circuiting *every*
  classification to `power_off` without ever taking a snapshot; the
  unchanged-frame cache could pin a wait loop to a stale `unknown` forever.
- **The MCP server didn't match its own safety story.** The destructive
  `power` tool's only gate was a boolean the calling model sets itself — with
  a refusal message that *coached* the model to set it. Snapshots came back as
  base64 text the model can't see; three of five tools crashed on the
  redfish/fake drivers the config explicitly routes to it.
- **Product/backlog**: the yanked `0.1.0a1` on PyPI documents features it
  doesn't contain; the repo had no description/topics; the structured-sensing
  surface (BootProgress/Sensors/Logs) was implemented but reachable from no
  entry point; three issues depended on an IPMI driver no issue tracked.

## What changed the same day

Ten commits landed on `main` (`a73f10b`…`5de030d`), all with tests, suite
green: the safety-ordering and HID/MSD gating fixes, delivery-aware retry +
error-taxonomy mapping in both transports, the vision robustness batch, config
typo-warnings (`admin`/`admin` lockout risk), `kvm-pilot eject`, the Redfish
transitional-PowerState fix, a full MCP-server hardening pass (capability-aware
per-call drivers, image snapshots, tool annotations, operator-side
`KVM_PILOT_MCP_ALLOW_POWER` gate, dry-run mode, local-VLM support, a 9-test
stdio suite), TLS pinning via `ssl_ca_file` ([#38][i38]), and a docs batch that
realigned every safety/config/packaging claim with the code. The complete
per-commit record is [#66][i66] (closed as the session's fix log).

Three deliberate breaking changes shipped while the API is alpha:
`snapshot()` lost its no-op `quality` parameter, HID/MSD writes became gated
destructive ops, and dry-run stopped invoking the confirm callback. Rationale
for each is in [decisions.md](../decisions.md).

## What was filed

**Issues [#37–#65][issues]** — one per verified finding (grouped only where
the root cause was shared), each carrying the evidence with line numbers, spec
citations, detailed follow-up instructions, and the verifier's independent
notes. Three milestones now express the strategy:

- **0.1.0a2 — installable alpha**: release-pipeline gates, supply-chain
  pinning, credential hygiene — what stands between today and an installable
  pre-release.
- **First hardware contact (GL-RM1PE)**: emulator fidelity, safety-guard test
  coverage, the exact client surface the first real run will exercise.
- **MCP flagship**: act-capability parity ([#61][i61] — agents can *see* but
  can't yet *drive*; the unattended-install loop is not yet possible over
  MCP), prompt-injection threat model ([#39][i39]), vision quality, and making
  the structured-sensing surface reachable ([#60][i60]).

Backlog hygiene from the PM pass: labels and milestones across the
pre-existing issues, re-scoping comments on the stale epics (#5, #6, #7, #13,
#17), #2 closed as delivered/superseded, repo description + topics set,
Discussions and private vulnerability reporting enabled.

## Where this leaves the project

The stated #1 risk is unchanged — **nothing has run against real hardware** —
but the failure modes most likely to bite on first contact (silent destructive
no-ops and double-fires, false `power_off` on ATX-less devices, raw-traceback
timeouts, stuck keys) now have fixes and regression tests, and the risk itself
has a milestone with a concrete work-list. The flagship gap for the chosen
audience is scoped in [#61][i61]. The next substantive decisions on record:
branch protection ([#56][i56], deferred) and cutting `0.1.0a2` once its
milestone clears.

[i38]: https://github.com/DustinTrap/kvm-pilot/issues/38
[i39]: https://github.com/DustinTrap/kvm-pilot/issues/39
[i56]: https://github.com/DustinTrap/kvm-pilot/issues/56
[i60]: https://github.com/DustinTrap/kvm-pilot/issues/60
[i61]: https://github.com/DustinTrap/kvm-pilot/issues/61
[i66]: https://github.com/DustinTrap/kvm-pilot/issues/66
[issues]: https://github.com/DustinTrap/kvm-pilot/issues?q=is%3Aissue+created%3A2026-07-01
