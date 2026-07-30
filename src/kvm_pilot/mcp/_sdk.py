"""One import site for the MCP SDK, across both SDK majors (#241, was #110).

mcp 2.x renamed the server class ``FastMCP`` to ``MCPServer`` and moved the
``mcp.server.fastmcp`` package to ``mcp.server.mcpserver``. That move is why the
dependency was pinned ``mcp<2``: a fresh ``pip install --pre kvm-pilot`` pulled
the 2.x beta and the bundled server failed to import (#110).

Everything kvm-pilot actually uses kept its name and signature across the move —
the ``tool``/``resource`` decorators, ``Context`` (``elicit``, ``session``,
``report_progress``), ``Image(data=, format=)``, ``ToolError``, and the
``mcp.types`` models — so a single shim covers both majors and lets the
dependency widen to ``mcp>=1.10,<3``. Import the SDK from here, never directly:
a stray ``from mcp.server.fastmcp import ...`` elsewhere silently reintroduces
#110 on mcp 2.x. ``tests/test_mcp_sdk_shim.py`` guards that.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations  # unmoved across majors; only the fields renamed

try:  # mcp 2.x
    from mcp.server.mcpserver import Context, Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    SDK_MAJOR = 2
except ModuleNotFoundError:  # mcp 1.x — FastMCP under its original name/path
    # no-redef: mypy sees both branches define these; only one ever executes, and
    # which one type-checks cleanly depends on the mcp installed in the checking env.
    from mcp.server.fastmcp import Context, Image  # type: ignore[no-redef]
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

    SDK_MAJOR = 1

# mcp 2.x also renamed the SDK's *model* fields to snake_case (`readOnlyHint` ->
# `read_only_hint`) while the protocol keys stayed camelCase. The two helpers below
# are the only places that care, so neither major's field names leak into the server.
_READ_ONLY_HINT = "read_only_hint" if SDK_MAJOR == 2 else "readOnlyHint"


def tool_annotations(
    *, read_only: bool, destructive: bool, idempotent: bool, open_world: bool
) -> ToolAnnotations:
    """Build a `ToolAnnotations` from the four hints, keyed by their wire names.

    Validated from a dict rather than passed as keywords on purpose: the wire keys
    are camelCase on both majors, but they are only *field* names on 1.x — keyword
    construction type-checks against whichever mcp is installed and fails on the
    other. `model_validate` accepts the wire key as a field name (1.x) or an alias
    (2.x), so one call site is correct and type-clean under both."""
    return ToolAnnotations.model_validate(
        {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        }
    )


def read_only_hint(annotations: object | None) -> bool | None:
    """A tool's declared ``readOnlyHint``, or None if it declares no annotations."""
    return None if annotations is None else getattr(annotations, _READ_ONLY_HINT, None)


__all__ = [
    "SDK_MAJOR",
    "Context",
    "Image",
    "MCPServer",
    "ToolError",
    "read_only_hint",
    "tool_annotations",
]
