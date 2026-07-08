#!/usr/bin/env bash
# checkpoint/scan.sh — READ-ONLY end-of-session sweep for the kvm-pilot project.
#
# Prints mechanical facts and MUTATES NOTHING. It never runs `git add`, never
# writes a file, never runs the (slow) test/lint bar. The agent interprets these
# facts against the rubric in SKILL.md. Safety property: if this script ever
# needs to change state, the design is wrong — keep it read-only.
set -uo pipefail

# --- locate the repo (session CWD is the parent; repo is ./kvm-pilot) ---------
REPO="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$REPO" ] && [ -d "$PWD/kvm-pilot/.git" ] && REPO="$PWD/kvm-pilot"
[ -z "$REPO" ] && { echo "FATAL: no git repo found at \$PWD or \$PWD/kvm-pilot"; exit 1; }
cd "$REPO"

MEM="$HOME/.claude/projects/-Users-dustintrap-Claude-kvm-pilot/memory"
PROTECTED="main"
SOT_DOCS=(README.md CHANGELOG.md src/kvm_pilot/mcp/README.md src/kvm_pilot/skill/SKILL.md)
GREEN_BAR='.venv/bin/ruff check . && .venv/bin/mypy src/kvm_pilot && .venv/bin/pytest -q'

echo "CHECKPOINT SCAN (read-only) — $(date '+%Y-%m-%d %H:%M') "
echo "REPO: $REPO"

# ============================================================ C1 VCS & tree ===
echo
echo "## C1  Version control & working tree"
BRANCH="$(git branch --show-current 2>/dev/null || echo '(detached)')"
echo "BRANCH: $BRANCH"
echo "HEAD: $(git log -1 --format='%h %s' 2>/dev/null)"
if [ "$BRANCH" = "$PROTECTED" ]; then echo "ON_PROTECTED_BRANCH: yes ($PROTECTED)"; else echo "ON_PROTECTED_BRANCH: no"; fi

DIRTY="$(git status --porcelain 2>/dev/null | grep -vE '^\?\?' || true)"
echo "UNCOMMITTED: $( [ -n "$DIRTY" ] && echo "$DIRTY" | wc -l | tr -d ' ' || echo 0 ) tracked file(s)"
[ -n "$DIRTY" ] && echo "$DIRTY" | sed 's/^/  /'

UNTRACKED="$(git status --porcelain --untracked-files=all 2>/dev/null | grep -E '^\?\?' | sed 's/^?? //' || true)"
echo "UNTRACKED_NOT_IGNORED: $( [ -n "$UNTRACKED" ] && echo "$UNTRACKED" | wc -l | tr -d ' ' || echo 0 )"
[ -n "$UNTRACKED" ] && echo "$UNTRACKED" | sed 's/^/  /'

UP="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
if [ -n "$UP" ]; then
  echo "UPSTREAM: $UP"
  echo "UNPUSHED (ahead): $(git rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo '?') commit(s)"
  echo "BEHIND: $(git rev-list --count 'HEAD..@{upstream}' 2>/dev/null || echo '?') commit(s)"
else
  echo "UPSTREAM: (none — branch not pushed)"
fi

# Stray debug in the working diff (added lines only). Bare print( is legit in this
# CLI, so only unambiguous markers are flagged; eyeball library-module prints.
DBG="$(git diff HEAD 2>/dev/null | grep -nE '^\+' | grep -nE 'breakpoint\(\)|pdb\.set_trace|import i?pdb|console\.log|debugger|\bFIXME\b|\bXXX\b' || true)"
echo "DEBUG_MARKERS_IN_DIFF: $( [ -n "$DBG" ] && echo "$DBG" | wc -l | tr -d ' ' || echo 0 )"
[ -n "$DBG" ] && echo "$DBG" | sed 's/^/  /'

# ==================================================== C2 Tracking & release ===
echo
echo "## C2  Tracking & the release/resume pointer"
VER="$(sed -nE 's/^__version__ = "(.*)"/\1/p' src/kvm_pilot/__about__.py 2>/dev/null)"
LOCAL_TAG="$(git tag --sort=-creatordate 2>/dev/null | head -1)"
# Releases are cut with `gh release` (tags the REMOTE), so local tags lag — ask gh
# for the authoritative latest release rather than trusting `git tag`.
REL="$(gh release list --limit 1 --json tagName -q '.[0].tagName' 2>/dev/null || true)"
echo "VERSION (__about__.py): ${VER:-?}"
echo "LATEST_RELEASE (gh, authoritative): ${REL:-'(gh unavailable)'}"
echo "LOCAL_LATEST_TAG: ${LOCAL_TAG:-none}"
if [ -n "$REL" ] && [ "$LOCAL_TAG" != "$REL" ]; then
  # CONTEXT, not an action: `gh release` tags only the remote, so local tags
  # always trail after a release. Cosmetic — nothing is at risk. (v0.2.0)
  echo "LOCAL_TAG_LAG: local $LOCAL_TAG trails remote $REL — EXPECTED after gh release; cosmetic, not work-at-risk"
fi
if [ -n "$REL" ] && [ "$REL" = "v${VER}" ]; then
  echo "RELEASE_STATE: version ${VER} == latest release ${REL} — nothing unreleased"
elif [ -n "$REL" ]; then
  echo "RELEASE_STATE: version ${VER} vs latest release ${REL} — differs (unreleased bump or newer tag)"
fi
# Resume pointer is the in-repo RESUME.md (git-tracked, committed). Flag likely-stale
# when work landed after RESUME.md was last touched — a hint, not a verdict. (v0.3.0)
if [ ! -f RESUME.md ]; then
  echo "RESUME.md: MISSING — the in-repo resume pointer does not exist; create it (mandatory handoff)"
else
  R_SHA="$(git log -1 --format='%h' -- RESUME.md 2>/dev/null)"
  if [ -z "$R_SHA" ]; then
    echo "RESUME_UNCOMMITTED: present but NEVER COMMITTED — commit it so the pointer is durable"
  else
    echo "RESUME.md: last committed $(git log -1 --format='%cs' -- RESUME.md 2>/dev/null) ($R_SHA)"
    NEWER="$(git rev-list --count "${R_SHA}..HEAD" 2>/dev/null || echo 0)"
    [ "${NEWER:-0}" -gt 0 ] && echo "RESUME_LIKELY_STALE: $NEWER commit(s) landed after RESUME.md's last commit — refresh it (hint)"
    # --porcelain shows untracked (??) too, so this catches working-tree edits.
    [ -n "$(git status --porcelain -- RESUME.md 2>/dev/null)" ] && echo "RESUME_UNCOMMITTED: edited since its last commit — commit it"
  fi
fi
echo "RECENT COMMITS (agent: pick out THIS session's):"
git log -15 --oneline 2>/dev/null | sed 's/^/  /'
echo "OPEN_PRS (this branch): $(gh pr list --head "$BRANCH" --json number,title -q '[.[]|"#\(.number) \(.title)"]|join("; ")' 2>/dev/null || echo '(gh unavailable)')"

# ==================================================== C3 Persistent memory ====
echo
echo "## C3  Persistent memory"
echo "MEMORY_DIR: $MEM"
if [ -d "$MEM" ]; then
  ORPHAN=""; MISSING=""
  for f in "$MEM"/*.md; do
    b="$(basename "$f")"; [ "$b" = "MEMORY.md" ] && continue
    grep -q "($b)" "$MEM/MEMORY.md" 2>/dev/null || ORPHAN="$ORPHAN $b"
  done
  while IFS= read -r link; do
    [ "$link" = "MEMORY.md" ] && continue
    [ -f "$MEM/$link" ] || MISSING="$MISSING $link"
  done < <(grep -oE '\(([a-z0-9-]+\.md)\)' "$MEM/MEMORY.md" 2>/dev/null | tr -d '()')
  echo "FILES_NOT_IN_INDEX:${ORPHAN:- none}"
  echo "INDEX_LINKS_MISSING_FILE:${MISSING:- none}"
  echo "NEWEST_MEMORY: $(ls -t "$MEM"/*.md 2>/dev/null | grep -v MEMORY.md | head -1 | xargs -I{} basename {} 2>/dev/null)"
else
  echo "(no memory dir — C3 n/a)"
fi

# ==================================================== C4 Docs reconciliation ==
echo
echo "## C4  Documentation (sources of truth)"
if grep -qE '^## \[Unreleased\]' CHANGELOG.md 2>/dev/null; then
  BODY="$(awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -vE '^\s*$' || true)"
  [ -z "$BODY" ] && echo "CHANGELOG_UNRELEASED: EMPTY" || echo "CHANGELOG_UNRELEASED: has content"
fi
if [ -n "${VER:-}" ]; then
  grep -qE "^## \[${VER}\]" CHANGELOG.md 2>/dev/null && echo "CHANGELOG_HAS_${VER}: yes" || echo "CHANGELOG_HAS_${VER}: NO"
fi
# Window = since the last LOCAL tag (may be wide if local tags lag; the agent
# narrows to THIS session's commits from C2). Falls back to last 15 commits.
WIN="${LOCAL_TAG:-HEAD~15}"; git rev-parse "$WIN" >/dev/null 2>&1 || WIN="HEAD~15"; git rev-parse "$WIN" >/dev/null 2>&1 || WIN="$(git rev-list --max-parents=0 HEAD | tail -1)"
echo "SOURCE-OF-TRUTH DOCS CHANGED SINCE ${WIN}:"
git diff --name-only "${WIN}..HEAD" -- "${SOT_DOCS[@]}" docs/ 2>/dev/null | sed 's/^/  /' | head -30 || true
echo "(a doc NOT changed this session is ⏭️ — do not audit it)"
echo "WIKI: auto-generated by .github/workflows/wiki-sync.yml — NEVER hand-edit the wiki"
# A code/dep change can make an UNCHANGED doc lie — the "changed-this-session" filter
# above won't catch it. Surface install-extra claims so the agent reconciles them
# against the CURRENT base deps (v0.4.0: ws->base #142 left README/cli.md/test-plan
# saying events "needs the ws extra" though websocket-client is now bundled).
echo "DOC_EXTRA_CLAIMS (reconcile vs BASE_DEPS — a promoted extra makes these lie):"
grep -rniE "\[(ws|totp)\]|(needs|requires) the .?(ws|totp).? extra|the .(ws|totp). extra" \
  README.md docs/*.md src/kvm_pilot/mcp/README.md src/kvm_pilot/skill/SKILL.md 2>/dev/null \
  | grep -viE "no-op alias|back-compat|bundled|base dep" | sed 's/^/  /' | head -10 || echo "  none"
echo "  BASE_DEPS: $(grep -E '^dependencies = ' pyproject.toml 2>/dev/null | sed 's/dependencies = //' || echo '?')"
# A new CLI subcommand missing from cli.md AND SKILL.md under-documents the shipped
# surface. The "changed-since-tag" filter above misses it when the command landed
# before the latest tag but after the doc's last edit (v0.5.0: a14 shipped
# route/host-exec absent from both, caught only by hand). Compares the argparse
# subcommands against the command names present in either doc.
echo "CLI_CMDS_UNDOCUMENTED (shipped subcommands absent from BOTH docs/cli.md and SKILL.md):"
CLI_CMDS=$(python3 -c 'import re,pathlib;print(" ".join(sorted(set(re.findall(r"add_parser\(\s*\"([a-z][a-z0-9-]*)\"", pathlib.Path("src/kvm_pilot/cli.py").read_text())))))' 2>/dev/null)
UNDOC=""
for c in $CLI_CMDS; do
  grep -qwF -- "$c" docs/cli.md 2>/dev/null || grep -qwF -- "$c" src/kvm_pilot/skill/SKILL.md 2>/dev/null || UNDOC="$UNDOC $c"
done
[ -n "$UNDOC" ] && echo "  ⚠️$UNDOC" || echo "  none"

# ==================================================== C5 Loose ends ===========
echo
echo "## C5  Loose ends"
echo "WORKTREES: $(git worktree list 2>/dev/null | wc -l | tr -d ' ') (>1 = stray)"
git worktree list 2>/dev/null | sed 's/^/  /'
echo "DOCKER_RUNNING: $(docker ps --format '{{.Names}}' 2>/dev/null | paste -sd, - || echo 'none/na')"
echo "LOCKS (.claude): $(find "$PWD/.claude" ../.claude -maxdepth 1 -name '*.lock' 2>/dev/null | paste -sd, - || echo none)"
echo "GREEN_BAR (NOT run — confirm this session's known-good, or run on demand):"
echo "  $GREEN_BAR"
# kvm-pilot mutates REAL hardware; device state isn't read-only-scannable, so this
# is a model-judged handoff note, not a scanned flag. (v0.2.0)
echo "DEVICE_STATE: (model) — did this session leave the fleet non-default? (keep-awake/jiggler on,"
echo "  appliance/target rebooted, media mounted, power changed) — if so, note it in the handoff."
echo
echo "SCAN COMPLETE (nothing was modified)."
