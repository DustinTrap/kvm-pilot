# Coverage policy — the ratchet

> **The floor only moves up.** It is a number committed to the repo, CI fails
> anything below it, and it is raised deliberately in the PR that earns it.

The two properties worth having here are not numeric: **coverage cannot silently
decrease**, and **gains cannot silently evaporate**. The absolute percentage
matters far less than the monotonic direction. A gate that can drift down is
worse than no gate, because it looks like assurance.

## How it works

| Piece | Where |
|---|---|
| The floor | [`.coverage-floor`](../.coverage-floor) — one number, at the repo root |
| The gate | [`scripts/coverage_gate.py`](../scripts/coverage_gate.py) — stdlib only |
| Its tests | [`tests/test_coverage_gate.py`](../tests/test_coverage_gate.py) — hermetic |
| The wiring | the `test` job in `.github/workflows/ci.yml`, immediately after pytest |

The test step already emits `coverage.xml`; the gate parses it, applies
exclusions, and compares **line** coverage to the floor. Below → the job fails.
At or above → it passes.

**Why a committed file** rather than a coverage service or a comparison against
the previous run's artifact: those add flakiness, races between merges, and an
audit hole. A file in the repo is deterministic, reviewable in the diff, and its
history is just `git log .coverage-floor`.

**Why no cron.** The measured thing only changes when code changes, so the
correct recurrence is every push/PR that touches code — which is exactly when
the required job already runs.

## Raising and lowering

**Raising** — edit `.coverage-floor` in the same PR that adds the tests. When a
run clears the floor by ≥1.0 point, CI prints a suggestion (in the job summary)
to set the floor to *measured − 0.5*. That suggestion reappears on **every green
run** until someone banks it; the nagging is the mechanism that makes the ratchet
actually move. It is never applied automatically — a human raises the floor,
because banking a gain is a judgement about whether the new tests are real.

**Lowering** — requires discussion on a tracking issue explaining why, linked
from the PR. Never a silent edit. If a deliberate deletion of dead code drops the
percentage, that is a legitimate lowering; it should still be visible and argued,
not slipped into a diff.

The 0.5-point buffer is not fussiness: coverage is deterministic per commit, but
ordinary refactors legitimately shift a few lines, and a floor set exactly at
measured flaps and trains people to ignore the gate.

## What is excluded, and what deliberately is not

Exclusions are applied **before** the percentage is computed and live inside the
gate script, so one file answers "what is measured?". An unexcluded denominator
is dishonest in both directions — it hides real gaps behind generated bulk, and
it punishes refactors of wiring that cannot be meaningfully tested.

Applied honestly to this repo, that is almost nothing:

| Pattern | Why | Matches today |
|---|---|---|
| `*/__about__.py` | version constant — metadata, not behavior | 1 file |
| `*/__main__.py` | entrypoint wiring | none |
| `*.g.py`, `*_pb2.py`, `*generated*` | codegen output | **none** |

**This repo has no generated code.** Nothing is codegen'd into `src/` — the wiki
is generated *from* `docs/`, not into the package — so the codegen patterns are
carried for future-proofing and match nothing.

Deliberately **not** excluded, though excluding them would flatter the number
considerably:

- `mcp/server.py` — the largest single gap (~324 uncovered lines)
- `cli.py` — the second largest (~243)

Both are real, testable surface, and both are partly tested. Hiding the two
biggest genuine gaps behind an "it's just wiring" label is precisely the
dishonesty the exclusion rule exists to prevent.

## Per-category reporting

The gate **reports** a per-category breakdown on every run — in the job summary,
worst first — while the pass/fail verdict stays the single blended number. This
is the cheap half of differential floors, and it is what tells us whether the
expensive half is worth building.

It exists because **a blended floor structurally cannot see a small
badly-covered area inside a large well-covered one.** At introduction:

| Category | Coverage | Lines | Uncovered | Target |
|---|---:|---:|---:|---:|
| CLI | 86.89% | 1053 | 138 | 85% ✅ |
| Vision | 87.87% | 371 | 45 | 90% |
| Health & evidence | 89.76% | 1562 | 160 | 90% |
| Drivers | 90.58% | 3322 | 313 | 90% ✅ |
| MCP server | 91.15% | 644 | 57 | 90% ✅ |
| In-band channels | 92.40% | 263 | 20 | 90% ✅ |
| Core plumbing | 92.62% | 1003 | 74 | 90% ✅ |
| **Safety & approval** | **98.21%** | 447 | 8 | **95%** ✅ |
| **Blended (the gate)** | **90.61%** | **8675** | **815** | — |

Six of eight categories are at or above target. The two that are not — Vision
and Health & evidence — sit within ~2 points and are not currently a priority.

The targets are deliberately **not uniform** — see below.

### The first thing the breakdown found was a measurement bug

`mcp/server.py` originally measured **49%**, which read as the worst-tested area
in the codebase. It was not. The MCP suite drives the server as a **real
subprocess over stdio** (82 tests), and a subprocess is invisible to the parent
process's coverage unless coverage restarts itself inside the child. Those 82
end-to-end tests were contributing **nothing** to the number.

Enabling subprocess measurement (`parallel` + `COVERAGE_PROCESS_START`, wired in
`tests/test_mcp_server.py`) moved it from 49% to **82.76%** — 215 lines
recovered without writing a single test.

That is worth remembering as a general caution: **a coverage number can be wrong
about which code is untested.** Chasing the original 49% with new tests would
have produced dozens of low-value tests for code that was already thoroughly
exercised. Measure what the number actually means before treating it as a
backlog.

Categories are defined in the gate script beside the exclusions, so one file
still answers "what is measured?". They are matched **first-match-wins**, which
is why `mcp/act.py` reports under *Safety & approval* rather than being absorbed
into *MCP server* — the approval path deserves its own line.

## Why one blended floor, for now

Differential floors — `safety.py` ≥90%, drivers ≥75%, CLI ≥60% — weight the gate
by where a regression actually hurts. They also cost roughly ten times the
machinery, and they are premature while the high-risk files already exceed the
floors they would be given.

### Why not 95% everywhere

A uniform 95% target is the wrong instrument, for three reasons this codebase
demonstrates:

1. **It weights a version constant the same as the approval gate.** A missed
   branch in `mcp/act.py` is a security gate that does not fire; a missed branch
   in argparse plumbing is nothing. The target should follow the blast radius —
   which is why *Safety & approval* is held to 95% and `cli.py` is not.
2. **The last few points are usually unprovokable branches.** Getting there means
   mocking transports into failure modes the real device cannot produce. Those
   tests assert *implementation*, so they lock in the code's shape and make
   honest refactors expensive — the opposite of what coverage is for.
3. **It creates an incentive to write assertion-free tests.** A test that
   executes a line without checking behavior raises coverage and adds no
   assurance. A uniform high bar is exactly the pressure that produces them.

The per-category targets in the table above are the alternative: high where a
regression is expensive, realistic where the remaining lines are plumbing.

**Where the MCP server stopped, and why.** Taking it from 82.76% to 91.15% was
worth doing — the tests that got it there pin real contracts (a closed gate
denies before any config probing; an unconfigured channel is an actionable error,
not a crash; disabling AMT user-consent needs its own gate). The 57 lines left
are a different animal: fragmented across **30 functions**, almost entirely
`except` branches for device failures that need a mocked transport to provoke,
plus receipt-denial paths reachable only by replaying a consumed receipt.
Covering them would mean mock-heavy tests asserting implementation. That is the
point at which more coverage buys less assurance, so the target is 90 and not 95.

**Revisit trigger.** Go granular when either happens:

1. A coverage-caused defect traces to a file whose contribution to the blended
   number hid it, or
2. `safety.py` or `mcp/act.py` — the gate/approval path — falls below 90%.

The gate now **reports** condition 2 automatically: a watched file under the
threshold prints a warning in the job summary. It does not fail the build —
tripping the trigger is meant to start a decision, not block a merge.

> **Trigger history.** Condition 2 was met the day it was written — `mcp/act.py`
> measured 89.53%. It was closed rather than argued away: tests for the
> elicitation path (accept / decline / cancel / prompt-failure), standing-grant
> scoping, and frame bookkeeping took it to **96.86%**, and the category to
> **98.21%**. That is the intended lifecycle — the trigger fires, someone writes
> the tests, the trigger stops firing.

The blended floor buys most of the value for a fraction of the machinery.

## Baseline

| | |
|---|---|
| Measured at introduction | 85.66% line coverage (7427 / 8670 lines, post-exclusion) |
| Initial floor | 85.0 |
| **Current** | **90.61%** (7860 / 8675) — subprocess measurement + approval-path, CLI and MCP-tool tests |
| **Current floor** | **90.11** |

Measuring the subprocess costs about **8% wall clock** on the suite (236s vs
218s locally) — worth it to stop 82 end-to-end tests reporting as nothing.

Note that pytest's headline figure is **branch** coverage and reads lower
(~83.7%). The gate measures **lines**; they are different measures, and mixing
them is how a floor comes to mean nothing. This is also why `fail_under` was
removed from `pyproject.toml` when the ratchet landed: two floors would be two
different numbers, and the weaker one firing first only obscures which control
spoke. `tests/test_coverage_gate.py` fails the build if a second floor
reappears, or if CI stops invoking the gate.

## Degenerate inputs

The gate exits `2` — distinct from a coverage failure — when it cannot honestly
answer:

- the floor file is missing, empty, or unparseable;
- the coverage report is missing or not XML;
- **the report has zero instrumented lines after exclusions.**

That last one is the gate catching its own plumbing breaking: it is what a test
step that silently ran *without* coverage looks like, and a naive gate would
report 0/0 as success.
