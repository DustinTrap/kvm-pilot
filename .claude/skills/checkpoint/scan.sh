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
  echo "TAG_LAG: local tags lag the remote — fix: git fetch --tags"
fi
if [ -n "$REL" ] && [ "$REL" = "v${VER}" ]; then
  echo "RELEASE_STATE: version ${VER} == latest release ${REL} — nothing unreleased"
elif [ -n "$REL" ]; then
  echo "RELEASE_STATE: version ${VER} vs latest release ${REL} — differs (unreleased bump or newer tag)"
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

# ==================================================== C5 Loose ends ===========
echo
echo "## C5  Loose ends"
echo "WORKTREES: $(git worktree list 2>/dev/null | wc -l | tr -d ' ') (>1 = stray)"
git worktree list 2>/dev/null | sed 's/^/  /'
echo "DOCKER_RUNNING: $(docker ps --format '{{.Names}}' 2>/dev/null | paste -sd, - || echo 'none/na')"
echo "LOCKS (.claude): $(find "$PWD/.claude" ../.claude -maxdepth 1 -name '*.lock' 2>/dev/null | paste -sd, - || echo none)"
echo "GREEN_BAR (NOT run — confirm this session's known-good, or run on demand):"
echo "  $GREEN_BAR"
echo
echo "SCAN COMPLETE (nothing was modified)."
