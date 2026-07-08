"""The interface-router scorecard profiler (#181).

Runs entirely against the in-memory FakeDriver with an injected clock, so it's
deterministic and touches no network — the benchmark logic (capability gating,
warm p50, failure capture, fastest-capable selection) is what's under test, not
real latency.
"""

from __future__ import annotations

import pytest

from kvm_pilot.benchmark import (
    CommandResult,
    Op,
    Scorecard,
    _measure,
    benchmark_driver,
)
from kvm_pilot.drivers.fake import FakeDriver


def make_clock(step: float = 0.001):
    """A fake perf clock: each call advances by ``step`` s, so every measured
    call reads as exactly ``step*1000`` ms — deterministic, no wall-clock."""
    t = [0.0]

    def clock() -> float:
        v = t[0]
        t[0] += step
        return v

    return clock


def _by_command(card: Scorecard) -> dict[str, CommandResult]:
    return {r.command: r for r in card.results}


def test_scorecard_charts_capable_and_unsupported_ops():
    d = FakeDriver(powered=True, phase="grub_menu")
    card = benchmark_driver(
        d, host="fake", driver_kind="fake", samples=6, clock=make_clock()
    )
    by = _by_command(card)

    # Every op the driver structurally supports is measured, capable, warm p50 set.
    for cmd in ("capabilities", "get_info", "get_logs", "is_powered_on",
                "snapshot", "boot_progress", "mouse_move"):
        assert by[cmd].capable, f"{cmd} should be capable on the fake driver"
        assert by[cmd].p50_ms == pytest.approx(1.0)  # step=0.001 s -> 1.0 ms per call
        assert by[cmd].samples == 6
        assert by[cmd].interface == "library"

    # A capability the driver lacks is charted (not silently skipped) with no
    # network call: capable False, no samples, an explanatory note.
    sensors = by["read_sensors"]
    assert sensors.capable is False
    assert sensors.p50_ms is None
    assert sensors.samples == 0
    assert "not supported" in sensors.note


def test_hid_probe_can_be_opted_out():
    d = FakeDriver(powered=True)
    card = benchmark_driver(d, host="fake", driver_kind="fake", hid=False, clock=make_clock())
    assert "mouse_move" not in _by_command(card)


def test_scorecard_metadata_and_serialization_round_trip():
    d = FakeDriver(powered=True)
    card = benchmark_driver(
        d, host="fake", driver_kind="fake", firmware="V1.9.1", samples=4, clock=make_clock()
    )
    assert card.host == "fake"
    assert card.driver == "fake"
    assert card.firmware == "V1.9.1"

    payload = card.to_dict()
    assert payload["host"] == "fake"
    assert payload["firmware"] == "V1.9.1"
    info = next(r for r in payload["results"] if r["command"] == "get_info")
    assert info["capable"] is True
    assert info["interface"] == "library"
    assert isinstance(info["p50_ms"], float)


def test_fastest_returns_lowest_latency_capable_row():
    card = Scorecard(
        host="h", driver="fake", firmware=None,
        results=[
            CommandResult("snapshot", "library", True, 50.0, 5),
            CommandResult("snapshot", "mcp", True, 20.0, 5),
            CommandResult("snapshot", "chrome", True, 2000.0, 5),
            CommandResult("get_info", "library", False, None, 0, "boom"),
        ],
    )
    # cheapest *capable* interface wins
    assert card.fastest("snapshot").interface == "mcp"
    # a command with no capable interface has no answer (router must fall back)
    assert card.fastest("get_info") is None
    # an unknown command likewise
    assert card.fastest("nonexistent") is None


def test_measure_records_failure_as_data_not_a_crash():
    def boom(_driver):
        raise RuntimeError("streamer returned H.264")

    result = _measure(Op("snapshot", None, boom), object(), samples=3, clock=make_clock())
    assert result.capable is False
    assert result.p50_ms is None
    assert result.samples == 0
    assert "RuntimeError" in result.note
    assert "H.264" in result.note
