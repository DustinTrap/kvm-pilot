---
name: checkpoint
description: >-
  End-of-session safety sweep for the kvm-pilot project — "is everything I did
  this session safe to leave?" Invoke at the end of a work session (or when the
  user says "checkpoint", "safe to stop?", "wrap up", "am I done here") to verify
  nothing is quietly unsaved: uncommitted/unpushed code, an unrecorded decision, a
  stale resume pointer, or a doc (README/CHANGELOG/mcp README/SKILL) that now lies
  about shipped behavior. Runs a read-only scan, reports a risk-ordered table with
  a one-line verdict, and always leaves a fresh resume handoff for the next session.
  It answers "nothing lost" only — NOT "is this releasable" and NOT the morning
  cloud/telemetry sweep.
---

# /checkpoint — end-of-session safety sweep

A checkpoint asks one question: **is everything I did this session durably captured, or is something quietly at risk of being lost?** The next session is often a fresh agent with zero memory of this one — a gap here means redone work or a decision shipped on a false assumption. This replaces "I think I saved everything" with a reproducible sweep ending in **SAFE TO STOP** or **N items need attention**, each with the exact fix.

It is the mirror of a morning sweep (which asks *what happened while I was away?*). Stay in that lane: checkpoint does **not** score releasability (a separate concern) and does **not** re-run cloud/telemetry health checks.

## Procedure

1. **Run the read-only scan** (it mutates nothing — that's the safety property):
   ```bash
   bash .claude/skills/checkpoint/scan.sh
   ```
2. **Interpret each surface against the rubric below** — the judgement calls the script can't make. Do not skip a surface because it "looked fine"; read the facts.
3. **Emit the risk-ordered table + one-line verdict** (format at the bottom). Highest-risk first.
4. **Always produce the resume handoff** (§ Mandatory handoff) — this happens even on an all-✅ scan.
5. **Offer the exact fix for every ⚠️**; run only the low-risk ones **on the operator's go** (commit-by-name, save-memory, fix-a-doc). Never promote/release; never blind-add.
6. **Meta-reflect and self-improve** (§ Self-improvement) — the last thing you do. Did the scan raise a **false positive** (flagged something fine), **miss** something you only caught by hand, or produce **noise**? Did the table help? If yes, amend `scan.sh`/`SKILL.md` *in this same run* to encode the fix and add a § Version history row pairing the change with the real incident. This step is why the skill is worth more each run.

## Rubric — interpret the scan, surface by surface (risk-ordered)

### C1 · Version control & working tree — *highest risk: unsaved code is unrecoverable*
- `UNCOMMITTED` > 0 → ⚠️ commit by name: `git -C kvm-pilot commit -m "…" -- <paths>`. **Never `git add -A`/`.`** (`.gitignore` covers venv/artifacts/`config.toml`/`.env`, but a stray secret or large file under a new name would be committed irreversibly — stage by name).
- `UNTRACKED_NOT_IGNORED` > 0 → ⚠️ eyeball each: is it source to track, or a stray artifact/secret to ignore/delete? Never bulk-add.
- `ON_PROTECTED_BRANCH: yes (main)` — **normal here, ✅, not a reason to branch.** kvm-pilot is issue-per-finding **direct-commit-to-`main`** (CLAUDE.md; agent work is not branched). The C1 risk is *unsaved work*, not the branch — do **not** advise `switch -c`. *(v0.2.0: the prior version wrongly assumed a feature-branch/PR flow.)*
- `UNPUSHED (ahead)` > 0 → ⚠️ **push it** (`git -C kvm-pilot push`). On this project pushing `main` runs CI and backs the work up; it does **not** release (a release needs `gh release create`). Commits left only-local are the actual work-at-risk. *(v0.2.0.)*
- `BEHIND` > 0 → ⚠️ note; rebase before more work.
- `DEBUG_MARKERS_IN_DIFF` > 0 (`breakpoint()`/`pdb`/`console.log`/`FIXME`/`XXX`) → ⚠️ remove before commit. Also eyeball the diff for stray `print(` added to **library** modules (`client.py`, `health.py`, drivers/ — the CLI uses `print` legitimately, so the scan doesn't flag it).

### C2 · Tracking & the resume pointer — *fold tracking + resume + release-state into ONE pass*
- **Tracked?** kvm-pilot is **issue-per-finding** (see CLAUDE.md): every meaningful change has a GitHub issue. From `RECENT COMMITS`, confirm this session's work references its issues. Untracked meaningful work → ⚠️ open/point at an issue.
- `LOCAL_TAG_LAG` present → **context, NOT a ⚠️ and NOT counted in the tally.** After `gh release` the local tags always trail the remote (which is authoritative); no work is at risk. Only run `git -C kvm-pilot fetch --tags` if you specifically need local tags. *(v0.2.0: this fired as a false positive on the first live run — a released session read as "1 item needs attention" when nothing did.)*
- `RELEASE_STATE` — context only; **checkpoint does not judge releasability.** "version == latest release" just means nothing is sitting unreleased.
- **Resume pointer** = `RESUME.md` (git-tracked). `RESUME_MISSING` → ⚠️ create it. `RESUME_LIKELY_STALE` (commits landed after its last update) or `RESUME_UNCOMMITTED` → ⚠️ refresh + commit + push it. The full verdict is § Mandatory handoff: is `RESUME.md` current vs. what actually happened this session? *(v0.3.0: pointer moved from agent memory to in-repo `RESUME.md`.)*

### C3 · Persistent memory — *judge worthiness, not just integrity*
- `FILES_NOT_IN_INDEX` / `INDEX_LINKS_MISSING_FILE` ≠ none → ⚠️ index broken; add the missing `- [Title](file.md) — hook` line, or fix the dangling link.
- **New-fact-worthiness** (the real judgement): did this session produce a durable fact not yet saved — a non-obvious decision, a "looks-wrong-but-intentional" choice, a corrected assumption, a hardware/device truth, standing user guidance? If yes → ⚠️ save it (one file, correct frontmatter `type:`, `**Why:**`/`**How to apply:**` for feedback/project) and index it in `MEMORY.md`. Don't save what the repo/git already records.

### C4 · Documentation reconciliation — *touch only what changed this session*
Sources of truth that can lie about shipped behavior: `README.md`, `CHANGELOG.md`, `src/kvm_pilot/mcp/README.md` (MCP tool table + env gates + safety model), `src/kvm_pilot/skill/SKILL.md` (bundled skill — interface matrix, tool list), `docs/decisions.md`, `__about__.py`, and the **Hardware-Compatibility wiki** (honesty about what's been exercised live).
- Only audit docs in `DOCS CHANGED SINCE …` **and** narrow to THIS session's commits (from C2). A doc that didn't change this session is ⏭️ — **except** when this session's *code/dependency* change makes an *unchanged* doc lie (below). *(v0.4.0)*
- **A code change can make an UNCHANGED doc lie** — the "changed-this-session" filter won't catch it, so don't stop there. `DOC_EXTRA_CLAIMS` lists install-extra references (`[ws]`/`[totp]`, "needs the ws extra") across the source-of-truth docs; reconcile each against `BASE_DEPS`. If this session promoted an extra to a base dep (or removed/renamed one), every doc still telling users to install that extra is ⚠️ — fix it even though the doc file itself didn't change. *(v0.4.0: `ws`→base (#142) left README/`cli.md`/`test-plan.md` saying `events` "needs the ws extra" though `websocket-client` is now bundled.)*
- `CHANGELOG_UNRELEASED: EMPTY` **and/or** `CHANGELOG_HAS_<ver>: NO` while a release shipped this session → ⚠️ the CHANGELOG lies about shipped behavior; add the version entry (move `[Unreleased]` items under a dated `## [<ver>]`).
- If MCP tools / CLI commands / gates changed this session, confirm `mcp/README.md` + `SKILL.md` match the shipped surface (tool table, `KVM_PILOT_MCP_ALLOW_*`, capability notes).
- Verified new hardware/behavior live this session (a device+capability combo) → ⚠️ record it in the Hardware-Compatibility wiki per the CLAUDE.md honesty rule; don't claim "tested" beyond what was exercised.
- **Never hand-edit the GitHub wiki** — it is auto-generated from `docs/` + `mcp/README.md` + `SKILL.md` by `wiki-sync`. Edit the sources.

### C5 · Loose ends — *cheap, last*
- **Device / hardware state left non-default** (kvm-pilot-specific — *this tool mutates real hardware*) → note it in the handoff, don't silently leave it. If the session enabled `keep-awake`/jiggler, rebooted an appliance or target, mounted media, or changed power, the fleet isn't in its resting state and the next session should know. The scan can't see this (querying devices isn't read-only-cheap) — it's a **model-judged handoff note**, not a scanned flag. *(v0.2.0: added after a session left the jiggler enabled on two units and rebooted a target, none of it captured.)*
- `WORKTREES` > 1 → ⚠️ stray worktree; `git worktree remove` if done.
- `DOCKER_RUNNING` ≠ none → ⚠️ the redfish-integration sushy-tools container may be left up; stop it if idle.
- Background jobs/agents from this session still running (check TaskList / your own shells) → ⚠️ stop them.
- `LOCKS` — `scheduled_tasks.lock` is the cron lock and is normal; note only if a checkpoint/agent lock looks stale.
- **Green bar** (`GREEN_BAR`) — **do not run it inside the sweep** (too slow). If this session already ran it green, mark ✅ "known-good this session"; otherwise ⚠️ "run on demand: `<GREEN_BAR>`". Never gate the checkpoint on a fresh full run.

## Mandatory handoff — always, even on an all-✅ scan

A checkpoint that ends with no fresh handoff **has not finished**. The resume pointer for this project is **`RESUME.md` at the repo root — a git-tracked, committed, pushed file** *(v0.3.0: moved here from the agent's auto-memory at the operator's request — the resume must live in git so it survives a fresh clone and any agent, not just this machine's Claude memory)*. Before signing off:

1. **Write or refresh `RESUME.md`** with: **current state** (branch @ sha, version, latest release, what landed, `[Unreleased]` empty?), **authoritative next steps** (what's in-flight, what's blocked on a human), **device state left non-default** (C5), and a pointer to `CLAUDE.md` for the standing rules. Stamp it `**Last updated:** <date> · <branch> @ <sha> · <version>`.
2. **Cross-check the handoff against live reality as you write it** (scan facts + `gh`): don't relist a closed issue/merged PR as open, don't call a done step "pending," don't name a file/flag that no longer exists. A hand-written summary regresses easily — verify each claim.
3. **Commit and push `RESUME.md`** (the one carve-out where the handoff mutates git — the scan flags `RESUME_UNCOMMITTED`/`RESUME_LIKELY_STALE`). A `RESUME.md` edited but not committed is not yet a durable pointer.
4. Durable *facts* (a hard-won gotcha, a corrected assumption, a device truth) still go to the agent memory (C3) — that's a **separate** store from the resume pointer; don't conflate them.

An all-✅ scan with a stale or missing `RESUME.md` is an **incomplete checkpoint** — refresh and commit it regardless.

## Hard rules (safety properties, not preferences)
- **Never blind-add.** No `git add -A` / `git add .`. Stage by name only. The scan is read-only and never adds.
- **No auto-mutation except the handoff.** Propose the exact command/edit for every ⚠️; run the safe ones only on the operator's go. **Never** merge to `main`, `gh release`, publish to PyPI, force-push, or edit the wiki — checkpoint answers "nothing lost," not "ship it."
- **Stay in your lane.** No releasability scoring, no cloud/telemetry sweep, no full test run inside the helper. Fold overlapping checks into one pass.

## Output format

```
CHECKPOINT — <branch> @ <short-sha> · <timestamp>
| #  | Surface                     | ✅/⚠️/⏭️ | Note / exact fix |
| C1 | VCS & working tree          | …       | … |
| C2 | Tracking & resume pointer   | …       | … |
| C3 | Persistent memory           | …       | … |
| C4 | Docs reconciliation         | …       | … |
| C5 | Loose ends                  | …       | … |
Bottom line: SAFE TO STOP  —or—  N items need attention (C2, C4).
```
Glyphs: ✅ clean · ⚠️ needs action · ⏭️ n/a this session. Table + one-line verdict; expand each ⚠️ into its exact command directly beneath the table. The bottom line must be consistent with the rows.

## Self-check before you sign off
Did every surface actually get interpreted (none skipped as "looked fine")? Is the resume-pointer verdict based on the pointer's *current* state vs. what happened this session? Did you judge memory *worthiness*, not just index integrity? Does every ⚠️ carry an exact command? Did you avoid every unapproved mutation and never suggest a blind-add? Is the bottom line a plain verdict consistent with the rows, and did the handoff actually get written? Did you run the § Self-improvement meta-reflection?

## What this deliberately leaves out
The scope is **"nothing lost," nothing more.** On purpose it does **not**:
- **Score releasability** — whether the code is *good enough to ship* is a separate concern (and releasing is a human call; #4's PyPI gate is intentionally out of band).
- **Run the green bar** (`ruff`/`mypy`/`pytest`) or any slow suite — a checkpoint is a seconds-long sweep; it reports whether *this session* already ran it green, it doesn't re-run it.
- **Do the morning sweep** — "what changed / is prod healthy / any telemetry alarms" is the *other* skill; checkpoint is the evening mirror.
- **Audit unchanged surfaces** — a doc/module untouched this session is ⏭️, never opened.
- **Verify hardware health** — that a device *works* is `healthcheck`'s job; checkpoint only *notes* device state left non-default (C5).

## Self-improvement
This skill is expected to be imperfect and to **harden every run**. Treat each misfire as a bug to fix on the spot:
- The **meta-reflection** (procedure step 6) is not optional — it is the mechanism. A false positive, a missed check, or noisy output is a defect in *this skill*, and the fix is an edit to `scan.sh`/`SKILL.md` in the same run.
- Every amendment **obeys the report-and-offer rule**: propose the diff, apply on confirmation. (An operator explicitly asking to *build/harden* the skill is that confirmation for that run.)
- Every amendment **adds a § Version history row** pairing the change with the *real incident* that motivated it — so the "why" is never lost and a future session doesn't reintroduce it. **The version-history table is this skill's memory of its own evolution**; read it at the top of a run to avoid re-fixing a known false positive.

## Version history
| Version | Change | Lesson / incident that drove it |
|---|---|---|
| 0.1.0 | Initial skill: five risk-ordered surfaces (VCS · tracking+resume · memory · docs · loose-ends), read-only `scan.sh`, mandatory memory-as-resume-pointer handoff, hard rules. | First cut (prior session). |
| 0.2.0 | **First live run (this session).** (1) `TAG_LAG` demoted from a ⚠️-with-fix to a neutral `LOCAL_TAG_LAG` context line + excluded from the tally. (2) C1 rubric rewritten for kvm-pilot's real **direct-commit-to-`main`** workflow — stop advising "move to a feature branch," and treat unpushed-`main` as ⚠️ (push runs CI/backs up; it isn't a release). (3) Added a **device-state** loose end (this tool mutates real hardware). (4) Added this **Self-improvement** section, the meta-reflection procedure step, an explicit **leaves-out** scope, and this table. | Run on a just-released session: `TAG_LAG` made an all-clean checkpoint read "1 item needs attention" (local tag `v0.1.0a10` trailing remote `v0.1.0a12` — expected after `gh release`); the branch rubric contradicted the project's own direct-to-`main` doctrine; and the session had left the jiggler on two units + rebooted a target with nothing capturing it. |
| 0.4.0 | **C4 now catches a doc that lies because of this session's *code/dependency* change, not only an edited doc.** Added a `DOC_EXTRA_CLAIMS` scan line (install-extra references across the source-of-truth docs) + `BASE_DEPS`, and a C4 rubric bullet to reconcile them — an extra promoted to base makes every "install `[x]`" note stale even though the doc file didn't change. | This session promoted `websocket-client` from the `ws` extra to a base dependency (#142); the scan's "docs changed this session" filter passed, but `README.md`/`docs/cli.md`/`docs/test-plan.md` still told users `events` "needs the ws extra" — a lie introduced by a *code* change, caught only by hand. |
| 0.3.0 | **Resume pointer moved from the agent auto-memory to an in-repo, git-tracked `RESUME.md`.** Mandatory-handoff section retargeted to write/commit/push `RESUME.md`; C2 rubric + `scan.sh` now check `RESUME.md` (present? `RESUME_LIKELY_STALE` if commits landed after its last commit; `RESUME_UNCOMMITTED` for never-committed *or* working-tree-edited). Durable *facts* still go to the memory (C3) — kept as a separate store. Also added a pointer to `RESUME.md` at the top of `CLAUDE.md` so a fresh session reads it first. | Operator: "I'd rather the resume be stored in git now, and moving forward" — the auto-memory resumes only *this machine's* Claude, not a fresh clone or another agent. *(Self-caught on the same run: the first `RESUME_UNCOMMITTED` check used `git diff`, which ignores untracked files, so a brand-new `RESUME.md` read as committed — fixed to use `git status --porcelain` + a never-committed branch.)* |
