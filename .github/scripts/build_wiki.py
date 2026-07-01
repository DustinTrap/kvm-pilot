#!/usr/bin/env python3
"""Build a GitHub-wiki-formatted mirror of the project documentation.

The canonical docs live in ``docs/`` (plus two guides that must stay next to the
code they document: ``skill/SKILL.md`` and ``mcp_server/README.md``). A GitHub
wiki is a flat namespace of pages with no subfolders, so this script copies each
source page to a flat output directory, rewrites intra-doc links for that flat
namespace (``architecture.md`` -> ``architecture``, ``docs/README.md`` -> the
wiki ``Home``), copies the diagrams alongside, and generates ``Home`` +
``_Sidebar`` navigation.

Run by ``.github/workflows/wiki-sync.yml``. Also runnable locally to preview the
output without touching the wiki::

    python .github/scripts/build_wiki.py --out /tmp/wiki && ls /tmp/wiki
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (repo-relative source, wiki page filename, sidebar title). Order = sidebar order.
# The wiki filename keeps the source stem so link rewriting is a plain ``.md``
# strip; ``Home`` is the wiki's reserved landing page.
PAGES: list[tuple[str, str, str | None]] = [
    ("docs/README.md", "Home.md", None),
    ("docs/architecture.md", "architecture.md", "Architecture"),
    ("docs/decisions.md", "decisions.md", "Design decisions"),
    ("docs/redfish.md", "redfish.md", "Redfish reference"),
    ("skill/SKILL.md", "skill.md", "Claude skill"),
    ("mcp_server/README.md", "mcp-server.md", "MCP server"),
    ("docs/CONTRIBUTING.md", "CONTRIBUTING.md", "Contributing"),
    ("docs/SECURITY.md", "SECURITY.md", "Security policy"),
]

# Link/image markdown: capture the ``[label]`` and the ``(target)`` separately.
_LINK = re.compile(r"(!?\[[^\]]*\])\(([^)]+)\)")
_IMAGE_EXTS = (".svg", ".png", ".jpg", ".jpeg", ".gif")


def _rewrite_target(target: str) -> str:
    """Map a link target as written in the repo to its flat-wiki equivalent."""
    if re.match(r"^(https?:|#|mailto:|/)", target):
        return target  # external, in-page anchor, or absolute — leave untouched
    path, _, anchor = target.partition("#")
    anchor = f"#{anchor}" if anchor else ""
    name = Path(path).name
    lower = name.lower()
    if lower.endswith(_IMAGE_EXTS):
        return name + anchor  # images are copied to the wiki root; keep basename
    # The two co-located guides map to their renamed wiki pages.
    if lower in ("skill.md",) or name == "SKILL.md":
        return "skill" + anchor
    if "mcp_server" in path or lower == "mcp-server.md":
        return "mcp-server" + anchor
    if name == "README.md":  # the docs index is the wiki landing page
        return "Home" + anchor
    if lower.endswith(".md"):
        return Path(path).stem + anchor
    return target


def _transform(md: str) -> str:
    return _LINK.sub(lambda m: f"{m.group(1)}({_rewrite_target(m.group(2))})", md)


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for src_rel, wiki_name, _ in PAGES:
        src = ROOT / src_rel
        if not src.exists():
            raise SystemExit(f"missing doc source: {src_rel}")
        (out / wiki_name).write_text(_transform(src.read_text()), encoding="utf-8")

    # Diagrams referenced by the pages, copied flat next to them.
    for svg in sorted((ROOT / "docs").glob("*.svg")):
        shutil.copy2(svg, out / svg.name)

    # Sidebar navigation, in PAGES order (Home first, then the guides).
    lines = ["### kvm-pilot docs", "", "- [[Home]]"]
    for _, wiki_name, title in PAGES:
        if title is None:
            continue
        lines.append(f"- [[{title}|{Path(wiki_name).stem}]]")
    (out / "_Sidebar.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"built {len(PAGES)} pages + sidebar into {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help="output directory for wiki pages")
    build(ap.parse_args().out)


if __name__ == "__main__":
    main()
