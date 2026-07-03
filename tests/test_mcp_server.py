"""Integration tests for the bundled MCP server (``kvm_pilot.mcp.server``).

These spawn the real server over stdio with the ``mcp`` SDK's client — the same
transport an MCP host uses — so they exercise the initialize handshake, tool
listing/annotations, and the safety gates end to end. The device is always the
in-process FakeDriver (selected via a temp config file); no network, no hardware.

The ``mcp`` SDK is a base dependency (it ships with the wheel), so this normally
runs; ``importorskip`` only guards a stripped-down install.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "info",
    "capabilities",
    "power_state",
    "logs",
    "snapshot",
    "classify_screen",
    "power",
    "healthcheck",
}


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A config file with a fake-driver profile and a (never-contacted) BMC one."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[hosts.fakebox]\ndriver = "fake"\nhost = "fakebox.local"\n\n'
        '[hosts.bmc]\ndriver = "redfish"\nhost = "bmc.invalid"\n'
    )
    return path


def server_env(config_file: Path, **extra: str) -> dict[str, str]:
    """Subprocess env: ambient KVM_PILOT_*/API keys stripped, ours layered on."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KVM_PILOT_") and k != "ANTHROPIC_API_KEY"
    }
    env["KVM_PILOT_CONFIG"] = str(config_file)
    env["KVM_PILOT_PROFILE"] = "fakebox"
    env.update(extra)
    return env


def run_session(env: dict[str, str], interact):
    """Spawn the server over stdio, initialize, run ``interact(session)``."""

    async def runner():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "kvm_pilot.mcp.server"], env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "kvm-pilot"
                return await interact(session)

    return asyncio.run(asyncio.wait_for(runner(), timeout=60))


def result_json(result) -> dict:
    """Parse a dict-returning tool's JSON text content."""
    assert result.content[0].type == "text"
    return json.loads(result.content[0].text)


def test_handshake_lists_annotated_tools(config_file):
    async def interact(session):
        return (await session.list_tools()).tools

    tools = run_session(server_env(config_file), interact)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED_TOOLS
    for name in EXPECTED_TOOLS - {"power"}:
        assert by_name[name].annotations.readOnlyHint is True, name
    power = by_name["power"].annotations
    assert power.readOnlyHint is False
    assert power.destructiveHint is True


def test_healthcheck_tool_returns_report(config_file):
    async def interact(session):
        return await session.call_tool("healthcheck", {})

    result = run_session(server_env(config_file), interact)
    parsed = result_json(result)
    assert parsed["driver"] == "fake"
    assert parsed["worst"] in {"OK", "INFO", "WARNING", "CRITICAL"}
    assert any(r["id"] == "recovery-path" for r in parsed["results"])


def test_power_errors_without_operator_gate(config_file):
    """The env gate is the floor: confirm=true alone must not fire the tool."""

    async def interact(session):
        return await session.call_tool("power", {"action": "off", "confirm": True})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    text = result.content[0].text
    assert "operator" in text
    # The refusal must not hand the agent a copy-pasteable incantation.
    assert "KVM_PILOT_MCP_ALLOW_POWER" not in text


def test_power_requires_confirm_as_second_factor(config_file):
    async def interact(session):
        return await session.call_tool("power", {"action": "off"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1")
    result = run_session(env, interact)
    assert result.isError is True
    assert "not confirmed" in result.content[0].text


def test_power_executes_on_fake_driver_when_fully_gated(config_file):
    async def interact(session):
        return await session.call_tool("power", {"action": "off", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1")
    result = run_session(env, interact)
    assert result.isError is False
    text = result.content[0].text
    assert "requested on host 'fakebox.local' (fake)" in text
    assert "DRY-RUN" not in text


def test_dry_run_marks_results_and_skips_the_command(config_file):
    async def interact(session):
        power = await session.call_tool("power", {"action": "off", "confirm": True})
        info = await session.call_tool("info", {})
        return power, info

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1", KVM_PILOT_MCP_DRY_RUN="1")
    power, info = run_session(env, interact)
    assert power.isError is False
    assert "DRY-RUN" in power.content[0].text
    assert "fakebox.local" in power.content[0].text
    # Read-only results carry the dry-run flag too.
    assert result_json(info)["dry_run"] is True


def test_read_only_tools_report_provenance(config_file):
    async def interact(session):
        info = await session.call_tool("info", {})
        state = await session.call_tool("power_state", {})
        return info, state

    info, state = run_session(server_env(config_file), interact)
    parsed = result_json(info)
    assert parsed["host"] == "fakebox.local"
    assert parsed["driver"] == "fake"
    # Regression for the get_atx_state AttributeError: power_state must work on
    # a driver without PiKVM's ATX detail endpoint.
    assert state.isError is False
    assert result_json(state)["powered_on"] is False


def test_capabilities_tool_lists_offline(config_file):
    """capabilities is structural — it lists what the driver supports with no
    network and no preflight, including for a BMC profile that is never contacted
    (host bmc.invalid). Output is in capability-enum declaration order."""

    async def interact(session):
        fake = await session.call_tool("capabilities", {})
        bmc = await session.call_tool("capabilities", {"profile": "bmc"})
        return fake, bmc

    fake, bmc = run_session(server_env(config_file), interact)
    parsed = result_json(fake)
    assert parsed["driver"] == "fake"
    caps = parsed["capabilities"]
    assert {"system_info", "power", "video", "logs"} <= set(caps)
    # Declaration order is stable: system_info precedes power precedes video.
    assert caps.index("system_info") < caps.index("power") < caps.index("video")
    # The BMC profile resolves and lists offline — it is never contacted.
    bmc_parsed = result_json(bmc)
    assert bmc_parsed["driver"] == "redfish"
    assert isinstance(bmc_parsed["capabilities"], list) and bmc_parsed["capabilities"]


def test_logs_tool_returns_text_with_provenance(config_file):
    """The logs tool wires get_logs() through, gated on Capability.LOGS, and
    carries provenance — it is the text diagnostic the image tools can't give."""

    async def interact(session):
        return await session.call_tool("logs", {"seek": 60})

    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    parsed = result_json(result)
    assert parsed["host"] == "fakebox.local"
    assert parsed["driver"] == "fake"
    assert isinstance(parsed["log"], str)


def test_snapshot_returns_a_real_image(config_file):
    async def interact(session):
        return await session.call_tool("snapshot", {})

    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    types = [c.type for c in result.content]
    assert "image" in types
    image = result.content[types.index("image")]
    assert image.mimeType == "image/jpeg"
    assert "fakebox.local" in result.content[0].text  # provenance note


def test_classify_screen_supports_local_backend(config_file):
    """The 'local' vision backend is selectable via the CLI's env var names.

    The fake driver is powered off, so the analyzer resolves the phase from the
    cheap power gate and never contacts the (nonexistent) VLM endpoint.
    """

    async def interact(session):
        return await session.call_tool("classify_screen", {})

    env = server_env(
        config_file,
        KVM_PILOT_VISION_BACKEND="local",
        KVM_PILOT_VISION_URL="http://127.0.0.1:9/v1",
        KVM_PILOT_VISION_MODEL="test-vlm",
    )
    result = run_session(env, interact)
    assert result.isError is False
    parsed = result_json(result)
    assert parsed["phase"] == "power_off"
    assert parsed["host"] == "fakebox.local"


def test_video_tools_error_cleanly_on_capability_less_driver(config_file):
    """A Redfish BMC has no video capture: the error must name driver and
    capability instead of AttributeError-ing (no network is ever contacted —
    the capability check is structural)."""

    async def interact(session):
        return await session.call_tool("snapshot", {"profile": "bmc"})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    text = result.content[0].text
    assert "redfish" in text
    assert "video" in text
