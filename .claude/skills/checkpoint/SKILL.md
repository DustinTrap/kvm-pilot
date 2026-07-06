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
5. **Offer the exact fix for every ⚠️**; run only the low-risk ones **on the operator's go** (commit-by-name, `git fetch --tags`, save-memory, fix-a-doc). Never promote/release; never blind-add.

## Rubric — interpret the scan, surface by surface (risk-ordered)

### C1 · Version control & working tree — *highest risk: unsaved code is unrecoverable*
- `UNCOMMITTED` > 0 → ⚠️ commit by name: `git -C kvm-pilot commit -m "…" -- <paths>`. **Never `git add -A`/`.`** (`.gitignore` covers venv/artifacts/`config.toml`/`.env`, but a stray secret or large file under a new name would be committed irreversibly — stage by name).
- `UNTRACKED_NOT_IGNORED` > 0 → ⚠️ eyeball each: is it source to track, or a stray artifact/secret to ignore/delete? Never bulk-add.
- `ON_PROTECTED_BRANCH: yes (main)` **with** uncommitted or new work → ⚠️ you're working on the release branch; move to a feature branch (`git -C kvm-pilot switch -c <branch>`). Being on `main` with a clean tree (just after a merge) is ✅.
- `UNPUSHED (ahead)` > 0 on a **feature** branch → ⚠️ offer to push it. (Don't push `main` beyond noting; that's release-adjacent.)
- `BEHIND` > 0 → ⚠️ note; rebase before more work.
- `DEBUG_MARKERS_IN_DIFF` > 0 (`breakpoint()`/`pdb`/`console.log`/`FIXME`/`XXX`) → ⚠️ remove before commit. Also eyeball the diff for stray `print(` added to **library** modules (`client.py`, `health.py`, drivers/ — the CLI uses `print` legitimately, so the scan doesn't flag it).

### C2 · Tracking & the resume pointer — *fold tracking + resume + release-state into ONE pass*
- **Tracked?** kvm-pilot is **issue-per-finding** (see CLAUDE.md): every meaningful change has a GitHub issue. From `RECENT COMMITS`, confirm this session's work references its issues. Untracked meaningful work → ⚠️ open/point at an issue.
- `TAG_LAG` present → ⚠️ local tags lag the remote (releases are cut via `gh release`, which tags only the remote). Safe fix: `git -C kvm-pilot fetch --tags`.
- `RELEASE_STATE` — context only; **checkpoint does not judge releasability.** "version == latest release" just means nothing is sitting unreleased.
- **Resume pointer** — judged in § Mandatory handoff (its own verdict): is the newest session memory current vs. what actually happened this session?

### C3 · Persistent memory — *judge worthiness, not just integrity*
- `FILES_NOT_IN_INDEX` / `INDEX_LINKS_MISSING_FILE` ≠ none → ⚠️ index broken; add the missing `- [Title](file.md) — hook` line, or fix the dangling link.
- **New-fact-worthiness** (the real judgement): did this session produce a durable fact not yet saved — a non-obvious decision, a "looks-wrong-but-intentional" choice, a corrected assumption, a hardware/device truth, standing user guidance? If yes → ⚠️ save it (one file, correct frontmatter `type:`, `**Why:**`/`**How to apply:**` for feedback/project) and index it in `MEMORY.md`. Don't save what the repo/git already records.

### C4 · Documentation reconciliation — *touch only what changed this session*
Sources of truth that can lie about shipped behavior: `README.md`, `CHANGELOG.md`, `src/kvm_pilot/mcp/README.md` (MCP tool table + env gates + safety model), `src/kvm_pilot/skill/SKILL.md` (bundled skill — interface matrix, tool list), `docs/decisions.md`, `__about__.py`, and the **Hardware-Compatibility wiki** (honesty about what's been exercised live).
- Only audit docs in `DOCS CHANGED SINCE …` **and** narrow to THIS session's commits (from C2). A doc that didn't change this session is ⏭️.
- `CHANGELOG_UNRELEASED: EMPTY` **and/or** `CHANGELOG_HAS_<ver>: NO` while a release shipped this session → ⚠️ the CHANGELOG lies about shipped behavior; add the version entry (move `[Unreleased]` items under a dated `## [<ver>]`).
- If MCP tools / CLI commands / gates changed this session, confirm `mcp/README.md` + `SKILL.md` match the shipped surface (tool table, `KVM_PILOT_MCP_ALLOW_*`, capability notes).
- Verified new hardware/behavior live this session (a device+capability combo) → ⚠️ record it in the Hardware-Compatibility wiki per the CLAUDE.md honesty rule; don't claim "tested" beyond what was exercised.
- **Never hand-edit the GitHub wiki** — it is auto-generated from `docs/` + `mcp/README.md` + `SKILL.md` by `wiki-sync`. Edit the sources.

### C5 · Loose ends — *cheap, last*
- `WORKTREES` > 1 → ⚠️ stray worktree; `git worktree remove` if done.
- `DOCKER_RUNNING` ≠ none → ⚠️ the redfish-integration sushy-tools container may be left up; stop it if idle.
- Background jobs/agents from this session still running (check TaskList / your own shells) → ⚠️ stop them.
- `LOCKS` — `scheduled_tasks.lock` is the cron lock and is normal; note only if a checkpoint/agent lock looks stale.
- **Green bar** (`GREEN_BAR`) — **do not run it inside the sweep** (too slow). If this session already ran it green, mark ✅ "known-good this session"; otherwise ⚠️ "run on demand: `<GREEN_BAR>`". Never gate the checkpoint on a fresh full run.

## Mandatory handoff — always, even on an all-✅ scan

A checkpoint that ends with no fresh handoff **has not finished**. The resume pointer for this project is the **project session memory** (`~/.claude/projects/-Users-dustintrap-Claude-kvm-pilot/memory/`, a `type: project` file, newest = top of `MEMORY.md`). Before signing off:

1. **Write or refresh** the current session's memory with three things: **current state** (branch @ sha, version, latest release, what landed, what's in-flight), **authoritative next steps**, and **standing rules** (issue-per-finding · `pip install` ships every surface · stdlib-only at core import · docs↔shipped parity · run `healthcheck` first · branch off `main` · never edit the wiki). Add its one-line pointer to the top of `MEMORY.md`.
2. **Cross-check the handoff against live reality as you write it** (scan facts + `gh`): don't relist a closed issue/merged PR as open, don't call a done step "pending," don't name a file/flag that no longer exists. A hand-written summary regresses easily — verify each claim.
3. If C2 shows an **open PR in flight**, also drop the resume state as a comment on that PR/issue so it's found from the tracker too.

An all-✅ scan with a stale resume pointer is an **incomplete checkpoint** — refresh the pointer regardless.

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
Did every surface actually get interpreted (none skipped as "looked fine")? Is the resume-pointer verdict based on the pointer's *current* state vs. what happened this session? Did you judge memory *worthiness*, not just index integrity? Does every ⚠️ carry an exact command? Did you avoid every unapproved mutation and never suggest a blind-add? Is the bottom line a plain verdict consistent with the rows, and did the handoff actually get written?
