#!/usr/bin/env python3
"""Build the kvm-pilot MCP Bundle (#148).

An ``.mcpb`` is a zip of the ``mcpb/`` directory: ``manifest.json`` at the root,
the entry-point shim, and a ``pyproject.toml`` the host resolves with uv. It
carries no vendored wheels — kvm-pilot depends on compiled packages whose wheels
are per-platform and per-Python, so bundling them would be both large and wrong
somewhere.

Stdlib only, so the release workflow needs nothing extra. Run from the repo root::

    python scripts/build_mcpb.py [--out dist/kvm-pilot-<version>.mcpb]

The version is read from ``src/kvm_pilot/__about__.py`` and **stamped into the
manifest and the dependency pin** at build time, so a release cannot ship a
bundle that installs a different kvm-pilot than it claims to be.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "mcpb"
ABOUT = ROOT / "src" / "kvm_pilot" / "__about__.py"

# Everything in mcpb/ ships except editor/OS noise.
EXCLUDE_NAMES = {"__pycache__", ".DS_Store"}

# Fixed member timestamp (the zip epoch) so a build is reproducible.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def package_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', ABOUT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"could not read __version__ from {ABOUT}")
    return m.group(1)


def stamp(version: str) -> tuple[str, str]:
    """Return (manifest.json, pyproject.toml) contents with ``version`` applied."""
    manifest = json.loads((BUNDLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version

    pyproject = (BUNDLE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = re.sub(r'^version = "[^"]+"', f'version = "{version}"', pyproject, count=1,
                       flags=re.M)
    pyproject = re.sub(r'"kvm-pilot==[^"]+"', f'"kvm-pilot=={version}"', pyproject, count=1)
    return json.dumps(manifest, indent=2) + "\n", pyproject


def build(out: Path) -> Path:
    version = package_version()
    manifest_text, pyproject_text = stamp(version)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Byte-identical for a given commit: sorted members, and a FIXED timestamp and
    # mode on every entry. `writestr` would otherwise stamp "now" while `write`
    # copies the source file's mtime, so two builds of one commit differed and the
    # artifact could not be compared across machines or re-verified later.
    files = sorted(
        p for p in BUNDLE_DIR.rglob("*")
        if p.is_file() and not any(part in EXCLUDE_NAMES for part in p.parts)
    )
    generated = {"manifest.json": manifest_text, "pyproject.toml": pyproject_text}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            rel = path.relative_to(BUNDLE_DIR).as_posix()
            info = zipfile.ZipInfo(rel, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            body = generated.get(rel)
            data = body.encode("utf-8") if body is not None else path.read_bytes()
            zf.writestr(info, data)

    names = zipfile.ZipFile(out).namelist()
    for required in ("manifest.json", "pyproject.toml", "server/main.py"):
        if required not in names:
            raise SystemExit(f"bundle is missing {required} — refusing to ship it")
    print(f"built {out} ({out.stat().st_size} bytes, {len(names)} files, v{version})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None,
                        help="output path (default dist/kvm-pilot-<version>.mcpb)")
    args = parser.parse_args(argv)
    out = args.out or (ROOT / "dist" / f"kvm-pilot-{package_version()}.mcpb")
    build(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
