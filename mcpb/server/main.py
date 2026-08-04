"""Entry point for the kvm-pilot MCP Bundle (.mcpb) — see ../manifest.json.

A thin shim on purpose. The bundle exists to remove an onboarding cliff, not to
become a second implementation: everything of substance lives in the installed
``kvm-pilot`` package, so a bundle user and a ``pip install`` user run byte-for-byte
the same server, with the same gates and the same audit trail.

The host resolves dependencies from the bundle's ``pyproject.toml`` with uv, which
pins one exact kvm-pilot version (#148).
"""

from __future__ import annotations

import sys


def main() -> None:
    try:
        from kvm_pilot.mcp.server import main as serve
    except ImportError as exc:  # pragma: no cover - install-time failure
        # stderr, because stdout is the MCP transport: anything written there
        # that is not a protocol frame corrupts the session.
        print(
            f"kvm-pilot is not importable inside this bundle ({exc}). The host "
            "installs it from the bundle's pyproject.toml with uv — reinstall the "
            "bundle, and if it persists please file an issue with the host's logs.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    serve()


if __name__ == "__main__":
    main()
