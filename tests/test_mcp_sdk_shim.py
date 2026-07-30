"""Guards on the MCP SDK import shim (#241, regression guard for #110).

`pip install --pre kvm-pilot` once pulled the mcp 2.x beta into a fresh env and
the bundled server failed to import, because mcp 2.x moved
`mcp.server.fastmcp` (FastMCP/Image) to `mcp.server.mcpserver` (MCPServer/Image).
The dependency now spans both majors (`mcp>=1.10,<3`) and every SDK symbol is
imported through `kvm_pilot.mcp._sdk`.

That only holds while nothing imports the SDK's server package directly, so the
first test below is the real guard: a stray `from mcp.server.fastmcp import ...`
anywhere in the shipped package breaks on mcp 2.x exactly the way #110 did, and
the equivalent 2.x-only import breaks on mcp 1.x.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kvm_pilot.mcp import _sdk

_SRC = Path(__file__).resolve().parents[1] / "src" / "kvm_pilot"
_SHIM = _SRC / "mcp" / "_sdk.py"


def _imports_mcp_server(source: str) -> bool:
    """Does this module import anything out of mcp's server package?

    Parsed rather than pattern-matched: the package is reachable as
    `import mcp.server.fastmcp`, `from mcp.server.mcpserver import X` *and*
    `from mcp.server import fastmcp`, and a regex that misses the last form
    would let exactly the #110 breakage back in. `mcp.types` is deliberately not
    covered — those models kept their path across the move, so importing them
    directly is fine.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) can't reach the mcp package.
            if node.level:
                continue
            module = node.module or ""
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
        else:
            continue
        if any(n == "mcp.server" or n.startswith("mcp.server.") for n in names):
            return True
    return False


def test_shipped_package_imports_the_sdk_only_through_the_shim():
    """No module but `_sdk` may touch mcp's server package (#110 regression)."""
    offenders = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if path != _SHIM and _imports_mcp_server(path.read_text())
    }
    assert not offenders, (
        f"{sorted(offenders)} import mcp's server package directly; import from "
        "kvm_pilot.mcp._sdk instead so both mcp 1.x and 2.x keep working (#241)."
    )


def test_the_guard_catches_every_way_of_reaching_mcp_server():
    """The guard above is only worth having if it sees all the import spellings.

    `from mcp.server import fastmcp` is the one a line-based check misses, and it
    breaks on mcp 2.x exactly like the others."""
    caught = [
        "import mcp.server.fastmcp",
        "import mcp.server.mcpserver as sdk",
        "from mcp.server.fastmcp import FastMCP",
        "from mcp.server.mcpserver.exceptions import ToolError",
        "from mcp.server import fastmcp",
        "from mcp.server import mcpserver",
        "def f():\n    from mcp.server.fastmcp import Context",  # function-local too
    ]
    for source in caught:
        assert _imports_mcp_server(source), f"guard missed: {source!r}"

    allowed = [
        "from mcp.types import ToolAnnotations",  # unmoved across majors
        "import mcp",
        "from kvm_pilot.mcp._sdk import MCPServer",
        "from . import server",  # relative: can't reach the mcp package
    ]
    for source in allowed:
        assert not _imports_mcp_server(source), f"guard over-reached: {source!r}"


def test_shim_exports_every_symbol_the_server_uses():
    """The shim is the whole SDK surface — each export must resolve on this major."""
    for name in ("Context", "Image", "MCPServer", "ToolError"):
        assert getattr(_sdk, name, None) is not None, f"_sdk.{name} did not resolve"
    assert _sdk.SDK_MAJOR in (1, 2)


def test_shim_resolves_the_major_actually_installed():
    """SDK_MAJOR must describe the installed mcp, not the branch that imported."""
    import mcp.server

    has_v2 = hasattr(mcp.server, "mcpserver")
    assert _sdk.SDK_MAJOR == (2 if has_v2 else 1)


def test_server_module_builds_its_app_from_the_shim():
    """The live server object is the shim's class on whichever major is installed."""
    from kvm_pilot.mcp import server

    assert isinstance(server.mcp, _sdk.MCPServer)
