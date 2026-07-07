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
    "wait_for_state",
    "power",
    "healthcheck",
    "ssh_reachable",
    "ssh_exec",
    "ssh_discover",
    "type_text",
    "press_key",
    "send_shortcut",
    "ctrl_alt_delete",
    "mouse",
    "mount_iso",
    "eject",
    "list_virtual_media",
}
# Tools that change state (readOnlyHint=False, destructiveHint=True).
DESTRUCTIVE_TOOLS = {
    "power", "ssh_exec", "type_text", "press_key", "send_shortcut", "ctrl_alt_delete",
    "mouse", "mount_iso", "eject",
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


def run_session_elicit(env, interact, *, action, content=None):
    """Like ``run_session`` but the client advertises elicitation and answers every
    approval prompt with a fixed ``(action, content)`` — the interactive posture."""
    from mcp.types import ElicitResult

    async def elicitation_callback(context, params):
        return ElicitResult(action=action, content=content)

    async def runner():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "kvm_pilot.mcp.server"], env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, elicitation_callback=elicitation_callback
            ) as session:
                await session.initialize()
                return await interact(session)

    return asyncio.run(asyncio.wait_for(runner(), timeout=60))


def test_handshake_lists_annotated_tools(config_file):
    async def interact(session):
        return (await session.list_tools()).tools

    tools = run_session(server_env(config_file), interact)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED_TOOLS
    for name in EXPECTED_TOOLS - DESTRUCTIVE_TOOLS:
        assert by_name[name].annotations.readOnlyHint is True, name
    for name in DESTRUCTIVE_TOOLS:
        ann = by_name[name].annotations
        assert ann.readOnlyHint is False, name
        assert ann.destructiveHint is True, name


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


# -- HID act tools (#61): effect gates, approval posture, receipt shape -------


def test_type_text_denied_without_hid_gate(config_file):
    # A denial comes back through the SAME call path (not a raised error) so the
    # agent can recover.
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hello", "confirm": True})

    parsed = result_json(run_session(server_env(config_file), interact))
    assert parsed["approved"] is False
    assert "disabled" in parsed["denied_reason"]


def test_type_text_preauthorized_with_confirm(config_file):
    # No elicitation client -> pre-authorized posture: standing ALLOW_HID + confirm.
    async def interact(session):
        return await session.call_tool("type_text", {"text": "root", "confirm": True})

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is True
    assert parsed["effect"] == "hid_input"
    assert parsed["transport"] == "hid.keyboard"
    assert parsed["op"] == "hid.type_text"
    assert parsed["invocation_id"]
    assert parsed["approval"]["args_hash"]
    assert parsed["approval"]["approver"] == "policy"


def test_type_text_denied_without_confirm_in_preauthorized(config_file):
    async def interact(session):
        return await session.call_tool("type_text", {"text": "x"})

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is False
    assert "confirm=true" in parsed["denied_reason"]


def test_ctrl_alt_delete_needs_power_gate_not_hid(config_file):
    # CAD is classified power_soft: ALLOW_HID must NOT suffice; ALLOW_POWER does.
    async def interact(session):
        return await session.call_tool("ctrl_alt_delete", {"confirm": True})

    hid_only = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert hid_only["approved"] is False
    powered = result_json(
        run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1"), interact)
    )
    assert powered["approved"] is True
    assert powered["effect"] == "power_soft"


def test_send_shortcut_cad_cannot_slip_the_hid_gate(config_file):
    async def interact(session):
        return await session.call_tool(
            "send_shortcut", {"keys": "ControlLeft,AltLeft,Delete", "confirm": True}
        )

    hid_only = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert hid_only["approved"] is False  # power_soft chord, HID gate insufficient
    powered = result_json(
        run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1"), interact)
    )
    assert powered["approved"] is True
    assert powered["effect"] == "power_soft"


def test_send_shortcut_vt_switch_uses_hid_gate(config_file):
    async def interact(session):
        return await session.call_tool(
            "send_shortcut", {"keys": "ControlLeft,AltLeft,F2", "confirm": True}
        )

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is True
    assert parsed["effect"] == "hid_control"


def test_act_interactive_elicit_accept(config_file):
    # An elicitation-capable client uses the interactive posture (no confirm needed).
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(
        run_session_elicit(env, interact, action="accept", content={"approve": True, "approver": "alice"})
    )
    assert parsed["approved"] is True
    assert parsed["approval"]["approver"] == "alice"


def test_act_interactive_elicit_decline_returns_same_path(config_file):
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(run_session_elicit(env, interact, action="decline"))
    assert parsed["approved"] is False
    assert "decline" in parsed["denied_reason"]


def test_act_interactive_elicit_accept_but_not_approved(config_file):
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(
        run_session_elicit(env, interact, action="accept", content={"approve": False})
    )
    assert parsed["approved"] is False


def test_allowlist_refuses_profile_not_listed(config_file):
    async def interact(session):
        return await session.call_tool("info", {"profile": "bmc"})

    env = server_env(config_file, KVM_PILOT_MCP_PROFILES="fakebox")
    result = run_session(env, interact)
    assert result.isError is True
    assert "allowlist" in result.content[0].text


def test_allowlist_allows_listed_profile(config_file):
    async def interact(session):
        return await session.call_tool("info", {})  # default profile 'fakebox' is listed

    env = server_env(config_file, KVM_PILOT_MCP_PROFILES="fakebox")
    assert run_session(env, interact).isError is False


# -- mouse + generation-keyed staleness (#124) -------------------------------


def test_snapshot_returns_frame_ref(config_file):
    async def interact(session):
        return await session.call_tool("snapshot", {})

    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["host"] == "fakebox.local"
    assert payload["frame_ref"].startswith("fakebox.local:0:")
    assert any(c.type == "image" for c in result.content)


def test_mouse_move_only_needs_no_ref(config_file):
    async def interact(session):
        return await session.call_tool("mouse", {"x": 0.5, "y": 0.5, "confirm": True})

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is True
    assert parsed["op"] == "hid.mouse_move"
    assert parsed["coord_space"] == "percent"


def test_mouse_click_requires_observed_frame_ref(config_file):
    async def interact(session):
        return await session.call_tool(
            "mouse", {"x": 0.5, "y": 0.5, "button": "left", "confirm": True}
        )

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is False
    assert "observed_frame_ref" in parsed["denied_reason"]


def test_mouse_click_with_fresh_ref_is_approved(config_file):
    async def interact(session):
        snap = await session.call_tool("snapshot", {})
        ref = json.loads(snap.content[0].text)["frame_ref"]
        return await session.call_tool(
            "mouse",
            {"x": 0.67, "y": 0.28, "button": "left", "observed_frame_ref": ref, "confirm": True},
        )

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is True
    assert parsed["op"] == "hid.mouse_click"


def test_mouse_click_refused_after_generation_bump(config_file):
    # A reboot (ctrl_alt_delete, power_soft) bumps the frame generation, so a click
    # planned against the pre-reboot frame is refused rather than landing blind.
    async def interact(session):
        snap = await session.call_tool("snapshot", {})
        ref = json.loads(snap.content[0].text)["frame_ref"]
        await session.call_tool("ctrl_alt_delete", {"confirm": True})
        return await session.call_tool(
            "mouse",
            {"x": 0.5, "y": 0.5, "button": "left", "observed_frame_ref": ref, "confirm": True},
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1", KVM_PILOT_MCP_ALLOW_POWER="1")
    parsed = result_json(run_session(env, interact))
    assert parsed["approved"] is False
    assert "stale screen" in parsed["denied_reason"]


def test_mouse_click_malformed_ref_refused(config_file):
    async def interact(session):
        return await session.call_tool(
            "mouse",
            {"x": 0.5, "y": 0.5, "button": "left", "observed_frame_ref": "garbage", "confirm": True},
        )

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"), interact))
    assert parsed["approved"] is False
    assert "valid frame reference" in parsed["denied_reason"]


# -- media tools (#61) -------------------------------------------------------


def test_mount_iso_denied_without_media_gate(config_file):
    async def interact(session):
        return await session.call_tool(
            "mount_iso", {"source": "/isos/ubuntu.iso", "confirm": True}
        )

    parsed = result_json(run_session(server_env(config_file), interact))
    assert parsed["approved"] is False
    assert "disabled" in parsed["denied_reason"]


def test_mount_iso_approved_with_media_gate(config_file):
    async def interact(session):
        return await session.call_tool(
            "mount_iso", {"source": "/isos/ubuntu.iso", "confirm": True}
        )

    parsed = result_json(
        run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_MEDIA="1"), interact)
    )
    assert parsed["approved"] is True
    assert parsed["effect"] == "media"
    assert parsed["transport"] == "msd"
    assert parsed["op"] == "msd.connect"


def test_eject_approved_with_media_gate(config_file):
    async def interact(session):
        return await session.call_tool("eject", {"confirm": True})

    parsed = result_json(
        run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_MEDIA="1"), interact)
    )
    assert parsed["approved"] is True
    assert parsed["op"] == "msd.disconnect"


def test_mount_iso_bumps_generation_invalidating_mouse_ref(config_file):
    # Mounting media changes the screen, so a click planned against the pre-mount
    # frame is refused (proves media effect bumps the frame generation).
    async def interact(session):
        snap = await session.call_tool("snapshot", {})
        ref = json.loads(snap.content[0].text)["frame_ref"]
        await session.call_tool("mount_iso", {"source": "http://h/x.iso", "confirm": True})
        return await session.call_tool(
            "mouse",
            {"x": 0.5, "y": 0.5, "button": "left", "observed_frame_ref": ref, "confirm": True},
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_MEDIA="1", KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(run_session(env, interact))
    assert parsed["approved"] is False
    assert "stale screen" in parsed["denied_reason"]


def test_ssh_exec_errors_without_operator_gate(config_file):
    """The env gate is the floor, checked before anything else (mirrors power)."""

    async def interact(session):
        return await session.call_tool("ssh_exec", {"command": "reboot", "confirm": True})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    text = result.content[0].text
    assert "operator" in text
    assert "KVM_PILOT_MCP_ALLOW_SSH" not in text  # no copy-pasteable incantation


def test_ssh_reachable_errors_when_not_configured(config_file):
    """The fake profile has no ssh_host — SSH-to-target must not be inferred."""

    async def interact(session):
        return await session.call_tool("ssh_reachable", {})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    assert "not configured" in result.content[0].text


def test_ssh_reachable_host_override_unblocks_unconfigured_profile(config_file):
    """host= lets a caller target a runtime-discovered address even when the
    profile has no ssh_host — the install-time DHCP case (#81)."""

    async def interact(session):
        return await session.call_tool("ssh_reachable", {"host": "127.0.0.1"})

    result = run_session(server_env(config_file), interact)
    assert result.isError is False  # no longer "not configured"
    parsed = result_json(result)
    assert parsed["target"] == "127.0.0.1"
    assert "reachable" in parsed


def test_ssh_reachable_rejects_hyphen_host(config_file):
    """A host starting with '-' is refused (ssh option-injection) — which also
    proves the host= override flows into channel construction."""

    async def interact(session):
        return await session.call_tool("ssh_reachable", {"host": "-oProxyCommand=touch /tmp/x"})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    assert "misparsed" in result.content[0].text


def test_ssh_discover_requires_confirm(config_file):
    """A network scan is risky — it must not run without an explicit acknowledgement."""

    async def interact(session):
        return await session.call_tool("ssh_discover", {"cidr": "10.0.0.0/30"})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    assert "not confirmed" in result.content[0].text


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


def test_list_virtual_media_inventories_msd_storage(config_file):
    """Read-only MSD inventory (#127): lets an agent find an ISO already on the
    device instead of asking the user to download/upload it again."""

    async def interact(session):
        return await session.call_tool("list_virtual_media", {})

    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    parsed = result_json(result)
    assert parsed["host"] == "fakebox.local"
    assert "online" in parsed["msd"] and "storage" in parsed["msd"]


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


def test_snapshot_reports_signal_and_flags_unchanged_frames(config_file):
    """#141/#143: the payload carries live signal state, and a byte-identical
    repeat frame is flagged so agents can spot stale/cached pixels."""

    async def interact(session):
        first = await session.call_tool("snapshot", {})
        second = await session.call_tool("snapshot", {})
        return first, second

    first, second = run_session(server_env(config_file), interact)
    p1, p2 = json.loads(first.content[0].text), json.loads(second.content[0].text)
    assert p1["signal"]["online"] is True and p1["signal"]["width"] == 1920
    assert p1["unchanged_since_last_snapshot"] is False
    # The fake driver returns a constant image, so the repeat is byte-identical.
    assert p2["unchanged_since_last_snapshot"] is True
    assert "stale" in p2["staleness_note"]


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
    assert parsed["mode"] == "server"


def test_classify_screen_cheap_gate_works_without_any_key(config_file):
    """Keyless deployments (#125) still get structured results for cheap-gate
    phases: the fake is powered off, so the power gate resolves with no vision
    credentials and no VLM call — the default anthropic backend is never used."""

    async def interact(session):
        return await session.call_tool("classify_screen", {})

    # server_env strips ANTHROPIC_API_KEY; the default backend is anthropic.
    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    parsed = result_json(result)
    assert parsed["mode"] == "server"
    assert parsed["phase"] == "power_off"


def test_classify_fallback_returns_image_and_prompt():
    """When server-side vision is unavailable, classify_screen hands the caller
    the screenshot + the prompt/schema to classify it itself (#125). Unit-level
    because the fake driver over MCP is always powered off (power gate resolves
    before the backend), so the fallback can't be reached end-to-end there."""
    from types import SimpleNamespace

    from mcp.server.fastmcp import Image

    from kvm_pilot.drivers import FakeDriver
    from kvm_pilot.errors import VisionError
    from kvm_pilot.mcp import server

    cfg = SimpleNamespace(host="fakebox.local", driver="fake")
    out = server._classify_fallback(
        cfg, FakeDriver(host="fakebox.local"), "look for a login prompt",
        VisionError("No Anthropic API key."),
    )
    assert isinstance(out, list) and len(out) == 2
    text, image = out
    payload = json.loads(text)
    assert payload["mode"] == "caller_classify"
    assert payload["host"] == "fakebox.local"
    assert "No Anthropic API key." in payload["reason"]
    assert payload["hint"] == "look for a login prompt"
    assert isinstance(payload["phases"], list) and payload["phases"]
    assert "phase" in payload["system_prompt"]  # the classification schema prompt
    assert isinstance(image, Image)


def test_classify_fallback_raises_when_snapshot_also_fails():
    """No image means nothing to delegate -> a clean tool error, not a fallback."""
    from types import SimpleNamespace

    from mcp.server.fastmcp.exceptions import ToolError

    from kvm_pilot.errors import VisionError
    from kvm_pilot.mcp import server

    class NoSnapshot:
        def snapshot(self):
            raise RuntimeError("streamer offline")

    cfg = SimpleNamespace(host="h", driver="fake")
    with pytest.raises(ToolError):
        server._classify_fallback(cfg, NoSnapshot(), "", VisionError("no key"))


# -- wait_for_state (#147): the MCP twin of CLI watch -------------------------


def test_wait_for_state_reaches_cheap_phase_without_vision_key(config_file):
    """A keyless server can still wait for cheap-gate phases: the fake is powered
    off, so the power gate resolves every poll with zero vision credentials and
    no VLM call — and the keyless pre-check must not block that. The success
    result mints a frame_ref (same convention as snapshot) so a follow-up mouse
    click can anchor to the final frame."""

    async def interact(session):
        return await session.call_tool("wait_for_state", {"phase": "power_off", "timeout": 10})

    # server_env strips ANTHROPIC_API_KEY; the default backend is anthropic.
    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    parsed = result_json(result)
    assert parsed["reached"] is True
    assert parsed["phase"] == "power_off"
    assert parsed["confidence"] >= 0.7
    assert parsed["waited_for"] == "power_off"
    assert parsed["host"] == "fakebox.local"
    assert parsed["frame_ref"].startswith("fakebox.local:0:")


def test_wait_for_state_timeout_returns_same_path_result(config_file):
    """A timeout is a result, not an error: reached=false plus the last observed
    state, so the agent can decide (and chain another call) instead of crashing."""

    async def interact(session):
        return await session.call_tool("wait_for_state", {"phase": "desktop", "timeout": 1})

    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    parsed = result_json(result)
    assert parsed["reached"] is False
    assert parsed["waited_for"] == "desktop"
    assert parsed["timeout_s"] == 1
    assert parsed["last"]["phase"] == "power_off"


def test_wait_for_state_rejects_unknown_phase_fast(config_file):
    """A typo'd phase token fails immediately with the valid list — it must not
    burn the requested timeout (the CLI watch guard, ported). The 300 s request
    completing inside the harness's 60 s cap proves the fast path."""

    async def interact(session):
        return await session.call_tool("wait_for_state", {"phase": "dekstop", "timeout": 300})

    result = run_session(server_env(config_file), interact)
    assert result.isError is True
    text = result.content[0].text
    assert "Valid phases" in text
    assert "desktop" in text


def test_wait_for_state_emits_progress(config_file):
    """Per-poll MCP progress notifications reach the client end-to-end over
    stdio — the anyio from_thread bridge out of the worker thread works."""
    updates: list[tuple] = []

    async def interact(session):
        async def cb(progress, total, message):
            updates.append((progress, total, message))

        return await session.call_tool(
            "wait_for_state", {"phase": "desktop", "timeout": 1}, progress_callback=cb
        )

    result = run_session(server_env(config_file), interact)
    assert result.isError is False
    assert updates
    progress, total, message = updates[0]
    assert total == 1.0
    assert "power_off" in message


def test_keyless_waitable_phases_gate_on_target_not_current_frame():
    """The decided keyless behavior (#147, revised after adversarial review):
    the gate is the set of phases this driver's cheap gates can EVER emit —
    computed from driver capabilities, never from classifying the current
    frame. Waiting keylessly for power_off must be allowed even while the
    screen shows VLM-only content; waiting keylessly for a VLM-only phase must
    be refused up front instead of burning the timeout."""
    from kvm_pilot.client import PiKVMDriver
    from kvm_pilot.drivers import FakeDriver
    from kvm_pilot.mcp import server
    from kvm_pilot.vision import ALL_PHASES

    # A driver with no cheap probes at all can wait for nothing keylessly.
    assert server._keyless_waitable_phases(object()) == set()
    # PiKVM family: power + signal probes, no structured BootProgress.
    pikvm = server._keyless_waitable_phases(PiKVMDriver("h"))
    assert pikvm == {"power_off", "no_signal"}
    assert "desktop" not in pikvm
    # BootProgress-capable drivers (BMCs, the fake) can report any phase token.
    assert server._keyless_waitable_phases(FakeDriver(host="x")) >= set(ALL_PHASES)


def test_wait_for_state_keyless_refusal_names_the_waitable_set(monkeypatch):
    """A keyless wait for a VLM-only phase fails fast with the typed pointer at
    caller-side classify_screen polling — the wrapped refusal text, unit-level
    (the MCP fake is BootProgress-capable, so end-to-end it can wait for
    anything keylessly)."""
    from kvm_pilot.mcp import server

    err = server._no_vision_error(
        "desktop",
        "no vision credentials configured; this driver's cheap gates can only "
        "observe: no_signal, power_off",
    )
    msg = str(err)
    assert "classify_screen" in msg and "caller-side" in msg
    assert "power_off" in msg  # the agent is told what IS waitable keylessly


def test_wait_timeout_clamped_to_cap_and_rejects_nonpositive():
    """The server-side ceiling without a 300 s test run: in-range values pass
    through, anything above is clamped to the cap; non-positive and non-finite
    (NaN compares False against <= 0) are refused."""
    from mcp.server.fastmcp.exceptions import ToolError

    from kvm_pilot.mcp import server

    assert server._clamp_timeout(10) == 10
    assert server._clamp_timeout(1e6) == 300.0
    assert server._clamp_timeout(1e6) == server._WAIT_TIMEOUT_CAP
    for bad in (0, -5, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ToolError):
            server._clamp_timeout(bad)


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
