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
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

# Expected annotations per tool, as (readOnlyHint, destructiveHint,
# idempotentHint, openWorldHint). All four are asserted explicitly (#195):
# clients build approval/parallelism policy from these bits and the spec
# defaults are punitive, so a new tool registered without a full annotation
# set must fail here, not ship. Rationale per profile lives next to the
# ToolAnnotations constants in ``kvm_pilot/mcp/server.py`` and in the
# annotations table in ``kvm_pilot/mcp/README.md``.
READ = (True, False, True, False)
READ_VISION = (True, False, True, True)  # server-side vision may be a cloud VLM
READ_VISION_WAIT = (True, False, False, True)  # ...and a timed wait isn't idempotent
DESTRUCTIVE = (False, True, False, False)
REVERSIBLE_WRITE = (False, False, True, False)
REVERSIBLE_WRITE_REMOTE = (False, False, True, True)

EXPECTED_ANNOTATIONS = {
    "info": READ,
    "healthcheck": READ,
    "capabilities": READ,
    "support_matrix": READ,
    "power_state": READ,
    "boot_options": READ,
    "logs": READ,
    "snapshot": READ,
    "classify_screen": READ_VISION,
    "wait_for_state": READ_VISION_WAIT,
    "list_virtual_media": READ,
    "ssh_reachable": READ,
    "ssh_discover": READ,
    "appliance_status": READ,
    "access_paths": READ,
    "doctrine": READ,
    "session": READ,
    "events": READ,
    "firmware_check": READ,
    "power": DESTRUCTIVE,
    "wake": DESTRUCTIVE,
    "set_boot_device": DESTRUCTIVE,
    "amt_enable": DESTRUCTIVE,
    "type_text": DESTRUCTIVE,
    "press_key": DESTRUCTIVE,
    "send_shortcut": DESTRUCTIVE,
    "ctrl_alt_delete": DESTRUCTIVE,
    "mouse": DESTRUCTIVE,
    "ssh_exec": DESTRUCTIVE,
    "appliance_reboot": DESTRUCTIVE,
    "mount_iso": REVERSIBLE_WRITE_REMOTE,
    "eject": REVERSIBLE_WRITE,
    "calibrate_mouse": REVERSIBLE_WRITE,  # pointer moves only; HID-gated all the same
    "file_firmware_report": REVERSIBLE_WRITE_REMOTE,
}
EXPECTED_TOOLS = set(EXPECTED_ANNOTATIONS)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A config file with a fake-driver profile and a (never-contacted) BMC one."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[hosts.fakebox]\ndriver = "fake"\nhost = "fakebox.local"\n\n'
        '[hosts.bmc]\ndriver = "redfish"\nhost = "bmc.invalid"\n'
    )
    return path


_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _coverage_active() -> bool:
    """True when this pytest run is collecting coverage."""
    try:
        import coverage

        return coverage.Coverage.current() is not None
    except Exception:  # noqa: BLE001 - absence of coverage is not an error
        return False


def server_env(config_file: Path, **extra: str) -> dict[str, str]:
    """Subprocess env: ambient KVM_PILOT_*/API keys stripped, ours layered on."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KVM_PILOT_") and k != "ANTHROPIC_API_KEY"
    }
    env["KVM_PILOT_CONFIG"] = str(config_file)
    env["KVM_PILOT_PROFILE"] = "fakebox"
    # The conftest cache isolation is monkeypatched env — stripped above, so the
    # spawned server would write the developer's real ~/.cache without this.
    env["KVM_PILOT_HEALTH_CACHE"] = str(config_file.parent / "health-cache.json")
    # Measure the SERVER, not just the client half. These tests spawn a real
    # subprocess, whose execution is invisible to the parent's coverage unless
    # coverage restarts itself inside the child — which coverage's sitecustomize
    # hook does when COVERAGE_PROCESS_START names a config (#246). Without this,
    # 82 end-to-end tests contributed nothing and mcp/server.py measured ~49%
    # while being thoroughly exercised. The quirk lives here, in the suite; the
    # coverage gate stays a dumb, honest calculator.
    if "COVERAGE_RUN" in os.environ or _coverage_active():
        env["COVERAGE_PROCESS_START"] = str(_PYPROJECT)
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
                # by_alias: mcp 2.x renamed these model fields to snake_case, but
                # the wire keys stayed camelCase — assert the wire, not the field
                # name, so one assertion holds on both SDK majors (#241).
                assert init.model_dump(by_alias=True)["serverInfo"]["name"] == "kvm-pilot"
                return await interact(session)

    return asyncio.run(asyncio.wait_for(runner(), timeout=60))


def result_json(result) -> dict:
    """Parse a dict-returning tool's JSON text content."""
    assert result.content[0].type == "text"
    return json.loads(result.content[0].text)


def wire(model, key: str):
    """Read a wire field off an SDK model, on either mcp major (#241).

    mcp 2.x renamed the model fields to snake_case (``isError`` -> ``is_error``,
    ``mimeType`` -> ``mime_type``) but kept the camelCase keys on the wire. Pass
    the wire key: it is what the protocol — and therefore the assertion — is
    actually about, and it reads the same on both majors."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    for field in (key, snake):
        if field in type(model).model_fields:
            return getattr(model, field)
    raise AssertionError(f"{type(model).__name__} has no {key!r}/{snake!r} field")


def is_error(result) -> bool:
    """The ``isError`` flag off a ``CallToolResult``, on either mcp major (#241)."""
    return wire(result, "isError")


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
    for name, expected in EXPECTED_ANNOTATIONS.items():
        ann = by_name[name].annotations
        assert ann is not None, name
        # by_alias keeps the camelCase wire keys across both mcp majors (#241).
        hints = ann.model_dump(by_alias=True)
        got = tuple(
            hints[k]
            for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
        )
        assert got == expected, f"{name}: annotations {got} != expected {expected}"


# Read-only launch mode (#196): the tools that survive the registration filter.
# ssh_discover is annotated read-only but is an active network scan — excluded
# from the least-privilege posture.
READ_ONLY_MODE_TOOLS = {
    name for name, (ro, _de, _idem, _ow) in EXPECTED_ANNOTATIONS.items() if ro
} - {"ssh_discover"}


def test_read_only_mode_registers_only_read_tools(config_file):
    """#196 layer 1: under READ_ONLY the destructive tools don't exist at all,
    even with ALLOW_* flags deliberately set — READ_ONLY wins."""

    async def interact(session):
        tools = (await session.list_tools()).tools
        health = await session.call_tool("healthcheck", {})
        power = await session.call_tool("power", {"action": "off", "confirm": True})
        return tools, health, power

    env = server_env(
        config_file,
        KVM_PILOT_MCP_READ_ONLY="1",
        KVM_PILOT_MCP_ALLOW_POWER="1",
        KVM_PILOT_MCP_ALLOW_HID="1",
    )
    tools, health, power = run_session(env, interact)
    assert {t.name for t in tools} == READ_ONLY_MODE_TOOLS
    assert result_json(health)["read_only"] is True
    assert is_error(power)  # never registered, so the call can only error


def test_read_only_mode_forces_gates_closed(monkeypatch):
    """#196 layer 2: every effect gate reads closed even with every ALLOW_* set,
    so a tool that slipped past the registration filter is still gate-denied."""
    from kvm_pilot.mcp import act
    from kvm_pilot.safety import EffectClass

    for flag in act.EFFECT_ENABLE_FLAG.values():
        if flag is not None:
            monkeypatch.setenv(flag, "1")
    monkeypatch.setenv("KVM_PILOT_MCP_READ_ONLY", "1")
    assert all(not act.gate_enabled(effect) for effect in EffectClass)


def test_read_only_mode_driver_denies_destructive(monkeypatch, config_file):
    """#196 layer 3: even handed an allow-all confirm, ``_driver`` builds the
    driver deny-all in read-only mode — a destructive call raises, never sends."""
    from kvm_pilot.mcp import server as server_mod
    from kvm_pilot.safety import SafetyError, allow_all

    # DEFAULT_CONFIG_PATH is resolved at import time, so in-process we patch the
    # module attribute (the stdio tests set KVM_PILOT_CONFIG pre-spawn instead).
    monkeypatch.setattr("kvm_pilot.config.DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setenv("KVM_PILOT_MCP_READ_ONLY", "1")
    monkeypatch.setenv("KVM_PILOT_SKIP_HEALTHCHECK", "1")
    monkeypatch.delenv("KVM_PILOT_MCP_DRY_RUN", raising=False)
    with pytest.raises(SafetyError):
        with server_mod._driver("fakebox", confirm=allow_all) as (_cfg, kvm):
            kvm.power_off()


def test_healthcheck_tool_returns_report(config_file):
    async def interact(session):
        return await session.call_tool("healthcheck", {})

    result = run_session(server_env(config_file), interact)
    parsed = result_json(result)
    assert parsed["driver"] == "fake"
    assert parsed["worst"] in {"OK", "INFO", "WARNING", "CRITICAL"}
    assert any(r["id"] == "recovery-path" for r in parsed["results"])


def test_session_posture_journal_and_breadcrumb(config_file):
    """#223: the re-anchor tool — clean posture before any act; after a gated
    power act and a wait, the journal and the last-wait breadcrumb appear."""

    async def interact(session):
        fresh = await session.call_tool("session", {})
        await session.call_tool("power", {"action": "off", "confirm": True})
        await session.call_tool("wait_for_state", {"phase": "power_off", "timeout": 10})
        resumed = await session.call_tool("session", {})
        return fresh, resumed

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1")
    fresh, resumed = run_session(env, interact)

    first = result_json(fresh)
    assert first["server"] == {
        "read_only": False, "dry_run": False,
        "approval_posture": "interactive", "profile_allowlist": None,
    }
    assert first["gates"] == {
        "power": True, "hid": False, "media": False, "config": False,
        "appliance": False, "external_write": False, "ssh": False,
        "consent_off": False,
    }
    assert first["recent_acts"] == []
    assert first["target"]["host"] == "fakebox.local"
    assert first["target"]["driver"] == "fake"
    assert first["target"]["frame_generation"] == 0
    assert first["target"]["last_wait"] is None

    second = result_json(resumed)
    acts = second["recent_acts"]
    # power rides the receipt pipeline since #234 — one consumed-receipt
    # terminal per act, in both the journal and the operator audit trail.
    assert [a["event"] for a in acts] == ["consumed"]
    assert acts[0]["tool"] == "power" and acts[0]["op"] == "power.off"
    assert acts[0]["dry_run"] is False
    # The power act bumped the frame generation; the wait left its breadcrumb.
    assert second["target"]["frame_generation"] >= 1
    wait = second["target"]["last_wait"]
    assert wait["phase"] == "power_off" and wait["reached"] is True
    # Gate posture never names the enabling env vars (#224).
    assert "KVM_PILOT" not in json.dumps(second["gates"])
    assert "KVM_PILOT" not in json.dumps(second["recent_acts"])


def test_session_never_refuses(config_file):
    """An empty allowlist locks down targets — the posture report still answers."""

    async def interact(session):
        return await session.call_tool("session", {})

    result = run_session(server_env(config_file, KVM_PILOT_MCP_PROFILES=""), interact)
    assert is_error(result) is False
    payload = result_json(result)
    assert payload["gates"]  # posture still served
    assert payload["server"]["profile_allowlist"] == []
    assert "error" in payload["target"]


def test_gate_closed_tool_denial_stays_mum(config_file):
    """#224 end-to-end: a closed-gate act denial carries no ALLOW_* incantation."""

    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi", "confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False and payload["outcome"] == "gate_closed"
    assert "KVM_PILOT" not in payload["denied_reason"]


def test_events_tool_bounded_collect(monkeypatch, config_file):
    """#233: the MCP events twin collects up to count and stops — never follows."""
    from kvm_pilot.drivers.fake import FakeDriver
    from kvm_pilot.mcp import server as server_mod

    monkeypatch.setattr("kvm_pilot.config.DEFAULT_CONFIG_PATH", config_file)

    def fake_watch(self, on_event=None, stream=True, timeout=None):
        for i in range(10):
            yield {"event_type": "atx_state", "event": {"n": i}}

    monkeypatch.setattr(FakeDriver, "watch_events", fake_watch)
    out = server_mod.events(count=3, profile="fakebox")
    assert out["count"] == 3  # stopped at count, not all 10
    assert [e["event"]["n"] for e in out["events"]] == [0, 1, 2]
    assert out["events"][0]["event_type"] == "atx_state"


def test_firmware_check_is_read_only_reconcile(monkeypatch, config_file, tmp_path):
    """#233: the read half of file_firmware_report — reports currency, files
    nothing, consults no gate."""
    from kvm_pilot.mcp import server as server_mod

    _fwc_env(monkeypatch, config_file, tmp_path)
    out = server_mod.firmware_check("fakebox")
    assert out["registry_behind"] is True  # the _fwc_env fixture registry is behind
    assert "installed" in out and "device_reported_latest" in out
    assert "filed" not in out and "approved" not in out  # read-only: no write half


def test_healthcheck_tool_uses_preflight_cache(monkeypatch, config_file):
    """#225: the explicit healthcheck runs through the preflight path — a repeat
    call serves stable checks from the HealthCache (only volatile checks re-run)
    and the audit satisfies the once-per-session preflight debounce."""
    from kvm_pilot import health
    from kvm_pilot.drivers import FakeDriver
    from kvm_pilot.mcp import server as server_mod

    monkeypatch.setattr("kvm_pilot.config.DEFAULT_CONFIG_PATH", config_file)

    batches: list[int] = []
    real = health.run_healthcheck

    def counting(driver, checks=None, **kw):
        batches.append(len(checks or []))
        return real(driver, checks=checks, **kw)

    monkeypatch.setattr(health, "run_healthcheck", counting)

    first = server_mod.healthcheck("fakebox")
    assert first["worst"] in {"OK", "INFO", "WARNING", "CRITICAL"}
    assert len(batches) == 2  # stable batch + volatile batch, both live

    second = server_mod.healthcheck("fakebox")
    assert second["worst"] == first["worst"]
    assert len(batches) == 3  # only the volatile batch re-ran; stable from cache

    # The explicit audit counts as this session's preflight for the device.
    assert health.preflight_once(FakeDriver(host="fakebox.local")) is None


def test_doctrine_serves_bundled_playbooks(config_file):
    """#222: ``doctrine`` re-serves the packaged skill so a session that never
    loaded the skill file — or compacted past it — can re-anchor in-band."""

    async def interact(session):
        listing = await session.call_tool("doctrine", {})
        recovery = await session.call_tool("doctrine", {"topic": "recovery"})
        bogus = await session.call_tool("doctrine", {"topic": "nope"})
        return listing, recovery, bogus

    listing, recovery, bogus = run_session(server_env(config_file), interact)
    topics = result_json(listing)["topics"]
    assert {"core", "recovery", "interfaces", "setup"} <= set(topics)
    text = result_json(recovery)["text"]
    assert "Wake-on-LAN" in text  # the ladder's first rung is the payload
    assert is_error(bogus)
    assert "recovery" in bogus.content[0].text  # refusal names the valid topics


def test_doctrine_also_served_as_resources(config_file):
    """#231: the same playbooks are MCP resources — no tool round-trip for
    resource-capable clients; byte-identical to the tool's text."""
    async def interact(session):
        templates = await session.list_resource_templates()
        # A plain str URI: mcp 1.x coerces it to AnyUrl, 2.x requires the str (#241).
        res = await session.read_resource("kvm-pilot://doctrine/recovery")
        tool = await session.call_tool("doctrine", {"topic": "recovery"})
        return templates, res, tool

    templates, res, tool = run_session(server_env(config_file), interact)
    assert any(
        wire(t, "uriTemplate") == "kvm-pilot://doctrine/{topic}"
        for t in wire(templates, "resourceTemplates")
    )
    assert res.contents[0].text == result_json(tool)["text"]


def test_power_errors_without_operator_gate(config_file):
    """The effect gate is the floor: confirm=true alone must not fire the tool.
    Since #234 the denial is same-path and typed, like every act tool."""

    async def interact(session):
        return await session.call_tool("power", {"action": "off", "confirm": True})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
    payload = result_json(result)
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"
    assert "operator" in payload["denied_reason"]
    # The refusal must not hand the agent a copy-pasteable incantation.
    assert "KVM_PILOT" not in payload["denied_reason"]


def test_amt_enable_requires_config_gate(config_file):
    """amt_enable is CONFIG_MUTATION — confirm=true alone must not fire it (#234:
    typed same-path denial)."""

    async def interact(session):
        return await session.call_tool("amt_enable", {"feature": "sol", "confirm": True})

    result = run_session(server_env(config_file), interact)  # ALLOW_CONFIG unset
    payload = result_json(result)
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"
    assert "disabled on this server" in payload["denied_reason"]


def test_amt_enable_consent_off_needs_dedicated_gate(config_file):
    """Disabling AMT user-consent is a surveillance escalation: it needs a SECOND
    operator gate beyond ALLOW_CONFIG (the user's 'allow behind a gate' choice)."""

    async def interact(session):
        return await session.call_tool(
            "amt_enable", {"feature": "kvm", "consent_off": True, "confirm": True}
        )

    # ALLOW_CONFIG on, but the dedicated consent-off gate is NOT — must still refuse.
    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_CONFIG="1")
    result = run_session(env, interact)
    assert is_error(result) is True
    text = result.content[0].text.lower()
    assert "consent" in text and "gate" in text


def test_power_requires_confirm_as_second_factor(config_file):
    async def interact(session):
        return await session.call_tool("power", {"action": "off"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1")
    payload = result_json(run_session(env, interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "not_confirmed"


def test_power_executes_on_fake_driver_when_fully_gated(config_file):
    async def interact(session):
        return await session.call_tool("power", {"action": "off", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1")
    result = run_session(env, interact)
    assert is_error(result) is False
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
    # The #149 remediation is for client-side elicitation outcomes only; a
    # closed-gate refusal must not suggest flipping the approval posture.
    assert parsed["remediation"] is None


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


def _run_counting_elicit(env, interact, *, content):
    """Like run_session_elicit but counts how many human prompts the server
    raised — the way to prove a standing grant SKIPPED a re-prompt (#192)."""
    from mcp.types import ElicitResult

    prompts: list = []

    async def elicitation_callback(context, params):
        prompts.append(params)
        return ElicitResult(action="accept", content=content)

    async def runner():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "kvm_pilot.mcp.server"], env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, elicitation_callback=elicitation_callback
            ) as session:
                await session.initialize()
                return await interact(session), prompts

    return asyncio.run(asyncio.wait_for(runner(), timeout=60))


def test_standing_approval_skips_reprompt_and_shows_in_session(config_file):
    """#192 full loop: one human approval with standing_minutes>0 opens a window;
    the next in-scope call (different args!) consumes it with NO second prompt,
    and the live grant shows in `session`."""

    async def interact(session):
        a = await session.call_tool("type_text", {"text": "first"})
        b = await session.call_tool("type_text", {"text": "second-different-args"})
        s = await session.call_tool("session", {})
        return a, b, s

    env = server_env(
        config_file, KVM_PILOT_MCP_ALLOW_HID="1", KVM_PILOT_MCP_STANDING_TTL="10"
    )
    (results, prompts) = _run_counting_elicit(
        env, interact, content={"approve": True, "standing_minutes": 5}
    )
    a, b, s = results
    pa, pb = result_json(a), result_json(b)
    assert pa["approved"] is True and pb["approved"] is True
    assert len(prompts) == 1  # ONLY the first call prompted a human
    assert pa["approval"]["via_standing_grant"] is None      # fresh approval opened it
    assert pb["approval"]["via_standing_grant"] is not None  # second consumed the grant
    grants = result_json(s)["standing_approvals"]
    assert len(grants) == 1
    assert grants[0]["effect"] == "hid_input" and grants[0]["expires_in_s"] > 0


def test_standing_approval_disabled_reprompts_every_call(config_file):
    """Without KVM_PILOT_MCP_STANDING_TTL the form's standing_minutes is ignored:
    the posture change is the operator's, never the client's."""

    async def interact(session):
        await session.call_tool("type_text", {"text": "one"})
        await session.call_tool("type_text", {"text": "two"})
        return await session.call_tool("session", {})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")  # no STANDING_TTL
    (s, prompts) = _run_counting_elicit(
        env, interact, content={"approve": True, "standing_minutes": 5}
    )
    assert len(prompts) == 2  # every call still prompts a human
    assert result_json(s)["standing_approvals"] == []


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
    assert parsed["remediation"] is None


def _assert_elicit_remediation(parsed):
    """#149: a client-side elicitation denial must explain itself — the action never
    reached the device — so the failure isn't mistaken for the host ignoring input.
    The ELICIT=off escape hatch is a security trade-off and is deliberately NOT
    named on a first, one-off failure (it appears after >=2 consecutive kills)."""
    assert parsed["approved"] is False
    assert "never reached the device" in parsed["remediation"]
    assert "KVM_PILOT_MCP_ELICIT=off" not in parsed["remediation"]


def test_act_interactive_elicit_decline_returns_same_path(config_file):
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(run_session_elicit(env, interact, action="decline"))
    assert parsed["approved"] is False
    assert "decline" in parsed["denied_reason"]
    assert parsed["outcome"] == "denied"        # typed (#149): an explicit no
    _assert_elicit_remediation(parsed)


def test_act_interactive_elicit_cancel_carries_remediation(config_file):
    # A chat client cancels a pending elicitation when a new message arrives (#149);
    # the denial must say the approval was cancelled client-side and is retryable.
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(run_session_elicit(env, interact, action="cancel"))
    assert parsed["denied_reason"] == "approval cancel"
    assert parsed["outcome"] == "cancelled"     # typed (#149): benign interruption
    assert "retryable" in parsed["remediation"]
    _assert_elicit_remediation(parsed)


def test_act_interactive_elicit_accept_but_not_approved(config_file):
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(
        run_session_elicit(env, interact, action="accept", content={"approve": False})
    )
    assert parsed["approved"] is False
    assert parsed["denied_reason"] == "denied by approver"
    _assert_elicit_remediation(parsed)


def test_allowlist_refuses_profile_not_listed(config_file):
    async def interact(session):
        return await session.call_tool("info", {"profile": "bmc"})

    env = server_env(config_file, KVM_PILOT_MCP_PROFILES="fakebox")
    result = run_session(env, interact)
    assert is_error(result) is True
    assert "allowlist" in result.content[0].text


def test_allowlist_allows_listed_profile(config_file):
    async def interact(session):
        return await session.call_tool("info", {})  # default profile 'fakebox' is listed

    env = server_env(config_file, KVM_PILOT_MCP_PROFILES="fakebox")
    assert is_error(run_session(env, interact)) is False


# -- mouse + generation-keyed staleness (#124) -------------------------------


def test_snapshot_returns_frame_ref(config_file):
    async def interact(session):
        return await session.call_tool("snapshot", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
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


def test_mouse_frame_age_guard_refuses_stale_and_unknown_refs():
    """#141: generation only bumps on power/media, so a frame can go stale while
    the screen changes on its own. The mouse guard refuses an observation older
    than the bound, and any ref this server never issued (fabricated / pre-restart)."""
    import time

    from kvm_pilot.mcp import act, server

    host = "frameage-test-host"  # unique so no other test's generation bump leaks in
    # A fresh, server-minted ref for the current generation passes every check.
    ref = act.frame_ref(host, b"\xff\xd8\xff-a-frame")
    assert server._mouse_stale(host, ref) is None
    # Age it past the bound -> refused as stale-by-age (generation still matches).
    with act._gen_lock:
        act._FRAME_MINTED[ref] = time.monotonic() - (server._MOUSE_FRAME_MAX_AGE + 30)
    stale = server._mouse_stale(host, ref)
    assert stale and "old" in stale
    # A well-formed ref with the right generation that this server never minted.
    unknown = f"{host}:{act.generation(host)}:deadbeefdeadbeef"
    unknown_reason = server._mouse_stale(host, unknown)
    assert unknown_reason and "not issued by this server" in unknown_reason


def test_mouse_frame_max_age_env_override(monkeypatch):
    monkeypatch.setenv("KVM_PILOT_MCP_FRAME_MAX_AGE", "5")
    from kvm_pilot.mcp import server

    assert server._mouse_frame_max_age() == 5.0
    monkeypatch.setenv("KVM_PILOT_MCP_FRAME_MAX_AGE", "nonsense")
    assert server._mouse_frame_max_age() == 60.0  # bad value -> safe default


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
    """The SSH effect gate is the floor, checked before any channel probing
    (#234: its own effect class — never the HID gate — and a typed denial)."""

    async def interact(session):
        return await session.call_tool("ssh_exec", {"command": "reboot", "confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"
    assert payload["effect"] == "ssh_exec"
    assert "KVM_PILOT" not in payload["denied_reason"]  # no incantation to relay


def test_ssh_reachable_errors_when_not_configured(config_file):
    """The fake profile has no ssh_host — SSH-to-target must not be inferred."""

    async def interact(session):
        return await session.call_tool("ssh_reachable", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is True
    assert "not configured" in result.content[0].text


def test_appliance_reboot_errors_without_operator_gate(config_file):
    """The appliance effect gate is the floor (#234: typed same-path denial)."""

    async def interact(session):
        return await session.call_tool("appliance_reboot", {"confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"
    assert "KVM_PILOT" not in payload["denied_reason"]  # no incantation to relay


def test_appliance_status_errors_when_not_enabled(config_file):
    """The fake profile has appliance_ssh off — the channel must not be inferred."""

    async def interact(session):
        return await session.call_tool("appliance_status", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is True
    assert "not enabled" in result.content[0].text


def test_access_paths_reports_the_lockout_view(config_file):
    """#162: access_paths rolls up the independent recovery paths + a summary."""

    async def interact(session):
        return await session.call_tool("access_paths", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
    parsed = result_json(result)
    assert "paths" in parsed and "summary" in parsed
    assert any(p["path"] == "kvmd-rest" for p in parsed["paths"])
    assert "out_of_band_live" in parsed["summary"]


def test_ssh_reachable_host_override_unblocks_unconfigured_profile(config_file):
    """host= lets a caller target a runtime-discovered address even when the
    profile has no ssh_host — the install-time DHCP case (#81)."""

    async def interact(session):
        return await session.call_tool("ssh_reachable", {"host": "127.0.0.1"})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False  # no longer "not configured"
    parsed = result_json(result)
    assert parsed["target"] == "127.0.0.1"
    assert "reachable" in parsed


def test_ssh_reachable_rejects_hyphen_host(config_file):
    """A host starting with '-' is refused (ssh option-injection) — which also
    proves the host= override flows into channel construction."""

    async def interact(session):
        return await session.call_tool("ssh_reachable", {"host": "-oProxyCommand=touch /tmp/x"})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is True
    assert "misparsed" in result.content[0].text


def test_ssh_discover_requires_confirm(config_file):
    """A network scan is risky — it must not run without an explicit acknowledgement."""

    async def interact(session):
        return await session.call_tool("ssh_discover", {"cidr": "10.0.0.0/30"})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is True
    assert "not confirmed" in result.content[0].text


def test_dry_run_marks_results_and_skips_the_command(config_file):
    async def interact(session):
        power = await session.call_tool("power", {"action": "off", "confirm": True})
        info = await session.call_tool("info", {})
        return power, info

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1", KVM_PILOT_MCP_DRY_RUN="1")
    power, info = run_session(env, interact)
    assert is_error(power) is False
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
    assert is_error(state) is False
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
    assert is_error(result) is False
    parsed = result_json(result)
    assert parsed["host"] == "fakebox.local"
    assert parsed["driver"] == "fake"
    assert isinstance(parsed["log"], str)


def test_list_virtual_media_inventories_msd_storage(config_file):
    """Read-only MSD inventory (#127): lets an agent find an ISO already on the
    device instead of asking the user to download/upload it again. Also forwards
    the driver's host-visible gadget name as ``host_visible_as`` (#78) so the
    agent knows which boot-menu entry proves the media is really presented."""

    async def interact(session):
        return await session.call_tool("list_virtual_media", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
    parsed = result_json(result)
    assert parsed["host"] == "fakebox.local"
    assert "online" in parsed["msd"] and "storage" in parsed["msd"]
    assert parsed["host_visible_as"] == "Fake Optical Drive"


def test_snapshot_returns_a_real_image(config_file):
    async def interact(session):
        return await session.call_tool("snapshot", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
    types = [c.type for c in result.content]
    assert "image" in types
    image = result.content[types.index("image")]
    assert wire(image, "mimeType") == "image/jpeg"
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
    assert is_error(result) is False
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
    assert is_error(result) is False
    parsed = result_json(result)
    assert parsed["mode"] == "server"
    assert parsed["phase"] == "power_off"


def test_classify_fallback_returns_image_and_prompt():
    """When server-side vision is unavailable, classify_screen hands the caller
    the screenshot + the prompt/schema to classify it itself (#125). Unit-level
    because the fake driver over MCP is always powered off (power gate resolves
    before the backend), so the fallback can't be reached end-to-end there."""
    from types import SimpleNamespace

    from kvm_pilot.drivers import FakeDriver
    from kvm_pilot.errors import VisionError
    from kvm_pilot.mcp import server
    from kvm_pilot.mcp._sdk import Image

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

    from kvm_pilot.errors import VisionError
    from kvm_pilot.mcp import server
    from kvm_pilot.mcp._sdk import ToolError

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
    assert is_error(result) is False
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
    assert is_error(result) is False
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
    assert is_error(result) is True
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
    assert is_error(result) is False
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
    # BootProgress-capable drivers (BMCs, the fake) can report any phase token
    # EXCEPT unknown (_probe_boot_progress never emits it).
    fake_waitable = server._keyless_waitable_phases(FakeDriver(host="x"))
    assert fake_waitable == set(ALL_PHASES) - {"unknown"}


class _StubContext:
    """Minimal MCP Context for calling a tool in-process: no elicitation
    capability, progress swallowed (a wait tool only needs report_progress,
    which is best-effort)."""

    class _Session:
        def check_client_capability(self, _cap) -> bool:
            return False

    def __init__(self) -> None:
        self.session = self._Session()

    async def report_progress(self, *a, **k) -> None:
        return None


def _run_tool(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


def test_wait_for_state_keyless_refuses_vlm_phase_end_to_end(config_file, monkeypatch):
    """The production keyless gate (#147), driven through the real tool in-process:
    an uncredentialed server + a driver whose cheap gates can't emit the target
    phase must raise the typed refusal — NOT fall through and burn the timeout.
    Kills the 'delete the gate block' mutation the subprocess fake can't (it is
    BootProgress-capable, so end-to-end it can wait for any phase keylessly)."""
    import kvm_pilot.config as _cfg
    from kvm_pilot.mcp import server
    from kvm_pilot.mcp._sdk import ToolError
    from kvm_pilot.vision.anthropic import AnthropicBackend
    monkeypatch.setattr(_cfg, "DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setenv("KVM_PILOT_PROFILE", "fakebox")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KVM_PILOT_MCP_DRY_RUN", "1")  # skip the preflight network path
    # Force the uncredentialed backend and the real PiKVM/GLKVM keyless reality
    # (only power_off/no_signal structurally observable).
    monkeypatch.setattr(server, "_keyless_waitable_phases",
                        lambda kvm: {server.PHASE_POWER_OFF, server.PHASE_NO_SIGNAL})
    monkeypatch.setattr(server, "_vision_backend", lambda: AnthropicBackend(model="pinned"))

    with pytest.raises(ToolError) as ei:
        _run_tool(server.wait_for_state(_StubContext(), phase="desktop", timeout=5))
    msg = str(ei.value)
    assert "classify_screen" in msg and "caller-side" in msg


def test_wait_for_state_keyless_reaches_cheap_phase_in_process(config_file, monkeypatch):
    """The keyless gate ALLOWS a cheap-gate phase: an uncredentialed server still
    waits for power_off on the (powered-off) fake, resolving it via the power
    gate with no VLM call."""
    import kvm_pilot.config as _cfg
    from kvm_pilot.mcp import server
    from kvm_pilot.vision.anthropic import AnthropicBackend
    monkeypatch.setattr(_cfg, "DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setenv("KVM_PILOT_PROFILE", "fakebox")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KVM_PILOT_MCP_DRY_RUN", "1")
    monkeypatch.setattr(server, "_vision_backend", lambda: AnthropicBackend(model="pinned"))

    out = _run_tool(server.wait_for_state(_StubContext(), phase="power_off", timeout=5))
    assert out["reached"] is True and out["phase"] == "power_off"


def test_wait_timeout_clamped_to_cap_and_rejects_nonpositive():
    """The server-side ceiling without a 300 s test run: in-range values pass
    through, anything above is clamped to the cap; non-positive and non-finite
    (NaN compares False against <= 0) are refused."""
    from kvm_pilot.mcp import server
    from kvm_pilot.mcp._sdk import ToolError

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
    assert is_error(result) is True
    text = result.content[0].text
    assert "redfish" in text
    assert "video" in text


# -- support_matrix (#102): offline evidence + derived maturity ---------------


def test_support_matrix_tool_returns_rm1pe_seed_offline(config_file):
    """Over real stdio, the tool answers from the ledger bundled in the package:
    the RM1PE combos including the seeded V1.5.1 firmware_update live FAIL
    (#94/#95) and each combo's #98-derived maturity — no device is contacted
    (the tool takes no profile; the config's only non-fake host is bmc.invalid,
    which would error loudly if touched)."""

    async def interact(session):
        return await session.call_tool("support_matrix", {"product": "RM1PE"})

    parsed = result_json(run_session(server_env(config_file), interact))
    by_fw = {c["firmware_version"]: c for c in parsed["combos"]}
    assert {"V1.5.1 release2", "V1.9.1 release1"} <= set(by_fw)
    old = by_fw["V1.5.1 release2"]
    assert old["vendor"] == "gl.inet" and old["product"] == "RM1PE"
    assert old["capabilities"]["firmware_update"]["status"] == "fail"
    # The derived maturity (#98) is joined from the shipped registry.
    assert old["maturity"]["capabilities"]["firmware_update"] == "alpha"
    assert by_fw["V1.9.1 release1"]["maturity"]["level"] == "beta"
    assert "UNVERIFIED" in parsed["note"]


def test_support_matrix_unknown_combo_returns_empty_cleanly(config_file):
    # An unknown device is an honest empty answer, not an error.
    async def interact(session):
        return await session.call_tool("support_matrix", {"vendor": "nonexistent"})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
    assert result_json(result)["combos"] == []


def test_capabilities_reports_live_evidence(config_file):
    """capabilities now carries driver-granular live_evidence (#102): which
    device+firmware combos this driver has ledger evidence for — [] for the
    fake driver — plus a pointer at support_matrix/healthcheck. Existing keys
    (driver, capability order) are unchanged."""

    async def interact(session):
        return await session.call_tool("capabilities", {})

    parsed = result_json(run_session(server_env(config_file), interact))
    assert parsed["driver"] == "fake"
    assert parsed["live_evidence"]["combos"] == []
    assert "support_matrix" in parsed["live_evidence"]["note"]


def test_wait_for_state_holds_display_awake(config_file, monkeypatch):
    """#161: wait_for_state wraps the poll loop in display_awake() — the
    jiggler is held ON for the wait and the prior state restored after, so the
    target can't DPMS-sleep mid-wait (the root of 'snapshot fails though video
    works')."""
    import kvm_pilot.config as _cfg
    from kvm_pilot.drivers.fake import FakeDriver
    from kvm_pilot.mcp import server
    from kvm_pilot.vision.anthropic import AnthropicBackend
    monkeypatch.setattr(_cfg, "DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setenv("KVM_PILOT_PROFILE", "fakebox")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KVM_PILOT_MCP_DRY_RUN", "1")
    monkeypatch.setattr(server, "_vision_backend", lambda: AnthropicBackend(model="pinned"))

    toggles: list[bool] = []
    orig = FakeDriver.set_jiggler

    def recording(self, active):
        toggles.append(active)
        return orig(self, active)

    monkeypatch.setattr(FakeDriver, "set_jiggler", recording)
    out = _run_tool(server.wait_for_state(_StubContext(), phase="power_off", timeout=5))
    assert out["reached"] is True
    assert toggles == [True, False]  # held for the wait, restored after (#161)


# -- file_firmware_report: the EXTERNAL_WRITE-gated emission tool (#190) ----- #


def _fwc_env(monkeypatch, config_file, tmp_path, *, registry_latest="V1.9.1 release1"):
    """Point the server at fakebox, teach FakeDriver the firmware surface, and
    stage a registry whose latest is ``registry_latest``."""
    import kvm_pilot.config as _cfg
    from kvm_pilot.drivers.fake import FakeDriver
    monkeypatch.setattr(_cfg, "DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setenv("KVM_PILOT_PROFILE", "fakebox")
    monkeypatch.setattr(FakeDriver, "get_firmware_info", lambda self: {
        "vendor": "gl.inet", "product": "RM1PE", "version": "V1.9.1 release1"},
        raising=False)
    monkeypatch.setattr(FakeDriver, "get_available_update", lambda self: {
        "current": "V1.9.1 release1", "latest": "V1.9.2 release1",
        "beta": None, "update_available": True}, raising=False)
    db = tmp_path / "reg.json"
    db.write_text(json.dumps({
        "schema_version": 2, "updated": "2026-07-02", "firmware": [
            {"vendor": "gl.inet", "product": "RM1PE", "latest": registry_latest,
             "source": "https://dl.gl-inet.com/kvm/rm1/stable", "date": "2026-07-02"}]}))
    monkeypatch.setenv("KVM_PILOT_FIRMWARE_DB", str(db))


def test_file_firmware_report_gate_closed_is_denial_not_error(config_file, tmp_path, monkeypatch):
    """Registry behind + gate unset: the reconcile half runs, the write half is
    a same-path denial naming the effect — never a raised error, and never the
    enabling env var (#224: refusals stay mum on the incantation)."""
    from kvm_pilot.mcp import server
    _fwc_env(monkeypatch, config_file, tmp_path)
    monkeypatch.delenv("KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE", raising=False)
    monkeypatch.setenv("KVM_PILOT_MCP_DRY_RUN", "1")
    out = _run_tool(server.file_firmware_report(_StubContext(), confirm=True))
    assert out["registry_behind"] is True
    assert out["approved"] is False
    assert "external_write" in out["denied_reason"]
    assert "KVM_PILOT" not in out["denied_reason"]


def test_file_firmware_report_registry_current_short_circuits(config_file, tmp_path, monkeypatch):
    """Registry already current: nothing to file — the gate is never consulted
    and no approval fields appear (this is the read half only)."""
    from kvm_pilot.mcp import server
    _fwc_env(monkeypatch, config_file, tmp_path, registry_latest="V1.9.2 release1")
    monkeypatch.setenv("KVM_PILOT_MCP_DRY_RUN", "1")
    out = _run_tool(server.file_firmware_report(_StubContext(), confirm=False))
    assert out["registry_behind"] is False and out["filed"] is False
    assert "already reflects" in out["reason"]
    assert "approved" not in out


def test_file_firmware_report_approved_dry_run_previews_body(config_file, tmp_path, monkeypatch):
    """Gate open + confirm + dry-run: the REAL helper renders the exact issue
    title/body the ingest workflow would receive, and nothing is sent."""
    from kvm_pilot.mcp import server
    _fwc_env(monkeypatch, config_file, tmp_path)
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE", "1")
    monkeypatch.setenv("KVM_PILOT_MCP_DRY_RUN", "1")  # forces inv.dry_run
    out = _run_tool(server.file_firmware_report(_StubContext(), confirm=True))
    assert out["approved"] is True and out["filed"] is False
    report = out["report"]
    assert report["dry_run"] is True
    assert "V1.9.2 release1" in report["title"]
    assert "### Vendor" in report["body"]          # the ingestable issue form


def test_file_firmware_report_gh_missing_is_graceful(config_file, tmp_path, monkeypatch):
    """Approved for real but no gh CLI in the server env — the documented
    failure mode: filed=false with an actionable reason, not a crash."""
    from kvm_pilot.mcp import server
    _fwc_env(monkeypatch, config_file, tmp_path)
    monkeypatch.setenv("KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE", "1")
    monkeypatch.delenv("KVM_PILOT_MCP_DRY_RUN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    out = _run_tool(server.file_firmware_report(_StubContext(), confirm=True))
    assert out["approved"] is True and out["filed"] is False
    assert "gh" in out["report"]["reason"]


# -- power tool: effect verification + generation bump (#168) ---------------- #


def test_power_result_reports_verified_effect(config_file):
    """The fake driver's power state genuinely flips, so the tool must report
    verified=true with the observed state — never just 'requested' (#168)."""
    async def interact(session):
        return await session.call_tool("power", {"action": "off", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1")
    parsed = result_json(run_session(env, interact))
    assert parsed["requested"] == "off"
    assert parsed["verified"] is True
    assert parsed["observed"] is False


def test_power_bumps_generation_invalidating_mouse_ref(config_file):
    """#168: a power action must invalidate pre-action frame refs, exactly like
    the ctrl_alt_delete path — a stale click must not land on the new screen."""
    async def interact(session):
        snap = await session.call_tool("snapshot", {})
        ref = json.loads(snap.content[0].text)["frame_ref"]
        await session.call_tool("power", {"action": "off", "confirm": True})
        return await session.call_tool(
            "mouse",
            {"x": 0.5, "y": 0.5, "button": "left", "observed_frame_ref": ref, "confirm": True},
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1", KVM_PILOT_MCP_ALLOW_POWER="1")
    parsed = result_json(run_session(env, interact))
    assert parsed["approved"] is False
    assert "stale screen" in parsed["denied_reason"]


def test_power_dry_run_skips_bump_and_verify(config_file):
    """Dry-run: nothing fired, so prior frame refs must stay valid and no
    verification claim is made."""
    async def interact(session):
        snap = await session.call_tool("snapshot", {})
        ref = json.loads(snap.content[0].text)["frame_ref"]
        power = await session.call_tool("power", {"action": "off", "confirm": True})
        mouse = await session.call_tool(
            "mouse",
            {"x": 0.5, "y": 0.5, "button": "left", "observed_frame_ref": ref, "confirm": True},
        )
        return power, mouse

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1", KVM_PILOT_MCP_ALLOW_POWER="1",
                     KVM_PILOT_MCP_DRY_RUN="1")
    power, mouse = run_session(env, interact)
    p = result_json(power)
    assert p["dry_run"] is True and p["verified"] is None
    m = result_json(mouse)
    assert m["approved"] is True          # ref still valid: no bump happened


def test_observe_power_honest_when_atx_sensing_absent():
    """GL/unwired-PiKVM shape: get_atx_state reports enabled=false — there is NO
    trustworthy signal, so the answer is None + why, never a fail-open guess."""
    import types

    from kvm_pilot.mcp.server import _observe_power, _verify_power

    class _GlIsh:
        def get_atx_state(self):
            return {"enabled": False, "leds": {"power": False}}
        def is_powered_on(self):  # fail-open base behavior — must NOT be consulted
            return True
        def known_quirks(self, firmware=None):  # the driver's own declaration
            return [types.SimpleNamespace(id="atx-power-state-always-off")]

    observed, source = _observe_power(_GlIsh())
    assert observed is None
    assert "atx-power-state-always-off" in source and "snapshot" in source
    verified, obs, note = _verify_power(_GlIsh(), "off", timeout=0.1)
    assert verified is None and obs is None


def test_verify_power_led_mismatch_is_false_not_silent():
    from kvm_pilot.mcp.server import _verify_power

    class _WiredStuck:
        def get_atx_state(self):
            return {"enabled": True, "leds": {"power": True}}  # never turns off

    verified, observed, note = _verify_power(_WiredStuck(), "off", timeout=0.6, poll=0.1)
    assert verified is False and observed is True
    assert "did NOT reach" in note


def test_verify_power_reset_reports_observed_only():
    from kvm_pilot.mcp.server import _verify_power

    class _Wired:
        def get_atx_state(self):
            return {"enabled": True, "leds": {"power": True}}

    verified, observed, note = _verify_power(_Wired(), "reset", timeout=0.1)
    assert verified is None and observed is True
    assert "no stable target" in note


# -- #149 remainder: typed outcomes + one-time consecutive-failure hint ------ #


def test_second_consecutive_cancel_surfaces_elicit_hint(config_file):
    """One cancel is a mis-click; two in a row on the same host is the #149
    pattern — only then does the remediation name the ELICIT=off trade-off."""
    async def interact(session):
        first = await session.call_tool("type_text", {"text": "hi"})
        second = await session.call_tool("type_text", {"text": "hi"})
        return first, second

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    first, second = run_session_elicit(env, interact, action="cancel")
    r1, r2 = result_json(first), result_json(second)
    assert "KVM_PILOT_MCP_ELICIT=off" not in r1["remediation"]
    assert "KVM_PILOT_MCP_ELICIT=off" in r2["remediation"]
    assert "disables per-call human approval" in r2["remediation"]
    assert "#2 in a row" in r2["remediation"]


def test_denial_outcomes_are_typed(config_file):
    """Agents branch on `outcome`, not on human-facing strings (#149)."""
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi", "confirm": True})

    # Gate closed.
    parsed = result_json(run_session(server_env(config_file), interact))
    assert parsed["outcome"] == "gate_closed"
    # Pre-authorized posture without confirm.
    async def no_confirm(session):
        return await session.call_tool("type_text", {"text": "hi"})

    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"),
                                     no_confirm))
    assert parsed["outcome"] == "not_confirmed"
    # Approved.
    parsed = result_json(run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1"),
                                     interact))
    assert parsed["outcome"] == "approved"


def test_act_result_carries_receipt_and_real_expiry(config_file):
    """#72 end-to-end over stdio: an approved act result carries the single-use
    receipt (consumed) and a real expiry instead of the old expires: null."""
    async def interact(session):
        return await session.call_tool("type_text", {"text": "hi", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    parsed = result_json(run_session(env, interact))
    assert parsed["approved"] is True
    assert parsed["receipt"]["state"] == "consumed"
    assert parsed["approval"]["expires"] is not None


# -- boot-device (BootSourceOverride, #201) — effect gate + confirm + execute --

def test_set_boot_device_errors_without_config_gate(config_file):
    async def interact(session):
        return await session.call_tool("set_boot_device", {"device": "pxe", "confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"
    assert "KVM_PILOT" not in payload["denied_reason"]  # no copy-pasteable incantation


def test_set_boot_device_requires_confirm(config_file):
    async def interact(session):
        return await session.call_tool("set_boot_device", {"device": "pxe"})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_CONFIG="1")
    payload = result_json(run_session(env, interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "not_confirmed"


def test_set_boot_device_executes_on_fake(config_file):
    async def interact(session):
        return await session.call_tool("set_boot_device", {"device": "pxe", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_CONFIG="1")
    result = run_session(env, interact)
    assert is_error(result) is False
    text = result.content[0].text
    assert '"target": "pxe"' in text and '"enabled": "Once"' in text


def test_boot_options_is_read_only(config_file):
    async def interact(session):
        return await session.call_tool("boot_options", {})

    result = run_session(server_env(config_file), interact)
    assert is_error(result) is False
    assert '"enabled": "Disabled"' in result.content[0].text


# -- wake / Wake-on-LAN (#199) — power gate + confirm + dry-run ---------------

def test_wake_errors_without_power_gate(config_file):
    async def interact(session):
        return await session.call_tool("wake", {"mac": "aa:bb:cc:dd:ee:ff", "confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"
    assert "operator" in payload["denied_reason"]


def test_wake_requires_confirm(config_file):
    async def interact(session):
        return await session.call_tool("wake", {"mac": "aa:bb:cc:dd:ee:ff"})

    result = run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1"), interact)
    payload = result_json(result)
    assert payload["approved"] is False
    assert payload["outcome"] == "not_confirmed"


def test_wake_requires_mac(config_file):
    async def interact(session):
        return await session.call_tool("wake", {"confirm": True})

    result = run_session(server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1"), interact)
    assert is_error(result) is True
    assert "no MAC" in result.content[0].text


def test_wake_dry_run_reports_without_sending(config_file):
    async def interact(session):
        return await session.call_tool("wake", {"mac": "aa:bb:cc:dd:ee:ff", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_POWER="1", KVM_PILOT_MCP_DRY_RUN="1")
    result = run_session(env, interact)
    assert is_error(result) is False
    text = result.content[0].text
    assert '"dry_run": true' in text and "aa:bb:cc:dd:ee:ff" in text


# ===========================================================================
# Tool paths that had no functional coverage (#246). These run through the real
# stdio session like the rest of this file, so they exercise the server as a
# client actually reaches it.
# ===========================================================================


def test_calibrate_mouse_is_gated_like_hid_input(config_file):
    """Pointer moves only — but it drives the live cursor for 10-30s, so it
    rides the HID gate. With the gate closed it must deny, not calibrate."""

    async def interact(session):
        return await session.call_tool("calibrate_mouse", {"confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False
    assert payload["outcome"] == "gate_closed"


def test_calibrate_mouse_dry_run_moves_nothing(config_file):
    """Pointer moves are NOT in DESTRUCTIVE_OPS, so the driver's own dry-run
    guard would not intercept them — the tool has to skip explicitly."""

    async def interact(session):
        return await session.call_tool("calibrate_mouse", {"confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1", KVM_PILOT_MCP_DRY_RUN="1")
    payload = result_json(run_session(env, interact))
    assert payload["approved"] is True
    assert payload["dry_run"] is True
    assert "would calibrate" in payload["detail"]


def test_calibrate_mouse_reports_a_calibration_failure_as_a_tool_error(config_file):
    """The fake driver's screen has no findable cursor, so calibration refuses.
    That must surface as an actionable ToolError, not an approval that lies."""

    async def interact(session):
        return await session.call_tool("calibrate_mouse", {"confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    result = run_session(env, interact)
    assert is_error(result)
    text = result.content[0].text.lower()
    # Names a precondition the operator can act on.
    assert "could not be decoded" in text or "snapshot" in text


def test_mouse_pixel_coords_need_a_driver_that_reports_resolution(config_file):
    """pixel/raw are the caller saying 'exactly here'; a driver without pixel
    support must say so rather than silently reinterpreting the coordinates."""

    async def interact(session):
        return await session.call_tool(
            "mouse", {"x": 100, "y": 100, "coord_space": "pixel", "confirm": True}
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    result = run_session(env, interact)
    assert is_error(result)
    assert "pixel" in result.content[0].text.lower()


def test_mouse_raw_coordinates_are_never_calibrated(config_file):
    """Raw is the escape hatch: no percent mapping, no stored correction."""

    async def interact(session):
        return await session.call_tool(
            "mouse", {"x": 10000, "y": 10000, "coord_space": "raw", "confirm": True}
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_HID="1")
    payload = result_json(run_session(env, interact))
    assert payload["approved"] is True
    assert payload.get("calibrated") is False


def test_ssh_discover_refuses_without_an_explicit_confirmation(config_file):
    """An active network scan is noisy and only acceptable on networks the user
    owns — the tool must not run one just because it was called."""

    async def interact(session):
        return await session.call_tool("ssh_discover", {"cidr": "10.0.0.0/30"})

    result = run_session(server_env(config_file), interact)
    assert is_error(result)
    assert "not confirmed" in result.content[0].text


def test_ssh_discover_rejects_an_over_broad_range(config_file):
    """A /8 is not a scan anyone meant to run."""

    async def interact(session):
        return await session.call_tool(
            "ssh_discover", {"cidr": "10.0.0.0/8", "confirm": True}
        )

    result = run_session(server_env(config_file), interact)
    assert is_error(result)
    assert "narrow" in result.content[0].text.lower() or "addresses" in result.content[0].text


def test_ssh_discover_rejects_a_malformed_cidr(config_file):
    async def interact(session):
        return await session.call_tool(
            "ssh_discover", {"cidr": "not-a-network", "confirm": True}
        )

    result = run_session(server_env(config_file), interact)
    assert is_error(result)


def test_ssh_discover_scans_a_tiny_range_when_confirmed(config_file):
    """A /31 on loopback is two addresses — proves the scan path runs without
    making this test a network probe of anything real."""

    async def interact(session):
        return await session.call_tool(
            "ssh_discover", {"cidr": "127.0.0.2/31", "port": 9, "confirm": True}
        )

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["cidr"] == "127.0.0.2/31"
    assert payload["port"] == 9
    assert isinstance(payload["candidates"], list)


def test_ssh_exec_gate_is_checked_before_any_config_probing(config_file):
    """The gate is the floor: a closed SSH gate must deny before the server even
    looks at whether ssh_host is configured."""

    async def interact(session):
        return await session.call_tool("ssh_exec", {"command": "uptime", "confirm": True})

    payload = result_json(run_session(server_env(config_file), interact))
    assert payload["approved"] is False and payload["outcome"] == "gate_closed"


def test_ssh_exec_missing_ssh_host_is_a_clean_tool_error(config_file):
    """Gate open, approved, then dispatch: an unconfigured channel surfaces as
    an actionable error (and is audited as a dispatch exception), not a crash."""

    async def interact(session):
        return await session.call_tool("ssh_exec", {"command": "uptime", "confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_SSH="1")
    result = run_session(env, interact)
    assert is_error(result)
    text = result.content[0].text
    assert "ssh_host" in text  # names the missing setting


def test_appliance_reboot_without_appliance_ssh_is_a_clean_tool_error(config_file):
    """Same shape for the appliance channel — opt-in, key-only, and absent here."""

    async def interact(session):
        return await session.call_tool("appliance_reboot", {"confirm": True})

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_APPLIANCE="1")
    result = run_session(env, interact)
    assert is_error(result)
    assert "appliance_ssh" in result.content[0].text


def test_amt_enable_on_a_non_amt_driver_says_so(config_file):
    """The fake driver has no AMT enablement; the tool must name the driver
    requirement rather than failing on a missing attribute."""

    async def interact(session):
        return await session.call_tool(
            "amt_enable", {"feature": "sol", "confirm": True}
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_CONFIG="1")
    result = run_session(env, interact)
    assert is_error(result)
    assert "amt driver" in result.content[0].text.lower()


def test_amt_consent_off_needs_its_own_gate(config_file):
    """Disabling KVM user-consent is a separate, deliberate posture change — the
    config gate alone must not authorize it (#233)."""

    async def interact(session):
        return await session.call_tool(
            "amt_enable", {"feature": "kvm", "consent_off": True, "confirm": True}
        )

    env = server_env(config_file, KVM_PILOT_MCP_ALLOW_CONFIG="1")
    result = run_session(env, interact)
    assert is_error(result)
    text = result.content[0].text.lower()
    assert "consent" in text


def test_observe_power_is_honest_when_the_atx_read_itself_fails():
    """An unreadable sensor is 'unverifiable', never a confident answer. The
    whole point of #149/#173: the tool must not convert a failed read into a
    power state it then reports as fact."""
    from kvm_pilot.errors import KVMPilotError
    from kvm_pilot.mcp.server import _observe_power

    class _Broken:
        def get_atx_state(self):
            raise KVMPilotError("ATX bus timeout")

    observed, source = _observe_power(_Broken())
    assert observed is None
    assert "unverifiable" in source
    assert "ATX bus timeout" in source  # carries the underlying cause


def test_preflight_gate_blocks_a_destructive_op_on_an_unacknowledged_critical(monkeypatch):
    """The intake gate is a gate: automation fails CLOSED on a critical nobody
    has acknowledged, and the tool error says why rather than proceeding."""
    from kvm_pilot import health as health_mod
    from kvm_pilot.health import HealthGateError
    from kvm_pilot.mcp import server as server_mod
    from kvm_pilot.mcp._sdk import ToolError

    # _preflight imports these lazily inside the function, so patch the source.
    monkeypatch.delenv("KVM_PILOT_MCP_DRY_RUN", raising=False)
    monkeypatch.delenv("KVM_PILOT_SKIP_HEALTHCHECK", raising=False)

    def blocked(*_a, **_kw):
        raise HealthGateError("no out-of-band recovery path")

    monkeypatch.setattr(health_mod, "preflight", blocked)

    with pytest.raises(ToolError) as ei:
        server_mod._preflight(object(), enforce=True)
    msg = str(ei.value)
    assert "preflight blocked this operation" in msg
    assert "no out-of-band recovery path" in msg  # names the finding


def test_informational_preflight_never_breaks_a_read(monkeypatch):
    """The non-enforcing audit is advisory: if it explodes, the read it was
    informing must still succeed."""
    from kvm_pilot import health as health_mod
    from kvm_pilot.mcp import server as server_mod

    monkeypatch.delenv("KVM_PILOT_MCP_DRY_RUN", raising=False)
    monkeypatch.delenv("KVM_PILOT_SKIP_HEALTHCHECK", raising=False)

    def boom(*_a, **_kw):
        raise RuntimeError("cache corrupted")

    monkeypatch.setattr(health_mod, "preflight_once", boom)
    # Returns rather than raising — an advisory audit is not a gate.
    assert server_mod._preflight(object(), enforce=False) is None
