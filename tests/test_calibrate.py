"""Mouse auto-calibration (#128) — synthetic-screen tests, no Pillow, no hardware.

The harness fakes the whole physical loop: a KVM whose ``mouse_move_percent``
applies a known distortion (the "OS") and whose ``snapshot`` encodes the
observed cursor position; the injected decoder renders that into a luminance
matrix with a cursor blob. ``run_calibration`` must recover the distortion,
verify a held-out point, and fail loudly on every dishonest input.
"""

from __future__ import annotations

import pytest

from kvm_pilot.calibrate import (
    GRID,
    PARK,
    CalibrationError,
    MouseCalibration,
    fit_axis,
    load_calibration,
    locate_change,
    maybe_apply,
    run_calibration,
    save_calibration,
)

W, H = 192, 108  # 16:9, fine enough that centroid quantization stays ~0.5%
BG, FG = 20, 220


def render(fx: float, fy: float) -> list[list[int]]:
    """A flat screen with a 3x3 cursor blob centered at the given fractions."""
    px, py = round(fx * (W - 1)), round(fy * (H - 1))
    frame = [[BG] * W for _ in range(H)]
    for y in range(max(0, py - 1), min(H, py + 2)):
        for x in range(max(0, px - 1), min(W, px + 2)):
            frame[y][x] = FG
    return frame


def decode(data: bytes) -> list[list[int]]:
    fx, fy = (float(v) for v in data.decode().split(","))
    return render(fx, fy)


class FakeScreenKVM:
    """Commanded → observed distortion stands in for OS pointer behavior."""

    def __init__(self, scale=0.9, offset=0.03, transform=None):
        self._observe = transform or (
            lambda c: min(1.0, max(0.0, scale * c + offset))
        )
        self.pos = (0.0, 0.0)
        self.moves: list[tuple[float, float]] = []

    def mouse_move_percent(self, x: float, y: float) -> None:
        self.moves.append((x, y))
        self.pos = (self._observe(x), self._observe(y))

    def snapshot(self) -> bytes:
        return f"{self.pos[0]:.6f},{self.pos[1]:.6f}".encode()


def calibrate(kvm, **kw):
    kw.setdefault("decoder", decode)
    kw.setdefault("settle", 0)
    kw.setdefault("sleep", lambda s: None)
    return run_calibration(kvm, host="testbox", **kw)


def test_recovers_linear_distortion_and_inverts():
    kvm = FakeScreenKVM(scale=0.9, offset=0.03)
    cal = calibrate(kvm)
    assert cal.scale_x == pytest.approx(0.9, abs=0.04)
    assert cal.offset_x == pytest.approx(0.03, abs=0.02)
    assert cal.scale_y == pytest.approx(0.9, abs=0.04)
    assert cal.residual <= 0.02
    # apply() must command the point that lands where the agent wanted.
    cx, cy = cal.apply(0.5, 0.5)
    assert 0.9 * cx + 0.03 == pytest.approx(0.5, abs=0.02)
    # The run ends with the cursor parked back at the corner.
    assert kvm.moves[-1] == PARK
    # And the fit used the full grid.
    assert len(kvm.moves) >= len(GRID) + 3  # park + grid + verify + re-park


def test_identity_mapping_calibrates_cleanly():
    cal = calibrate(FakeScreenKVM(scale=1.0, offset=0.0))
    assert cal.scale_x == pytest.approx(1.0, abs=0.03)
    assert cal.apply(0.4, 0.7)[0] == pytest.approx(0.4, abs=0.02)


def test_nonstatic_screen_refused():
    kvm = FakeScreenKVM()
    frames = iter(["0.5,0.5", "0.7,0.7", "0.2,0.2", "0.9,0.9"])
    kvm.snapshot = lambda: next(frames).encode()  # type: ignore[method-assign]
    with pytest.raises(CalibrationError, match="changing on its own"):
        calibrate(kvm)


def test_invisible_cursor_is_actionable():
    kvm = FakeScreenKVM()
    with pytest.raises(CalibrationError, match="cursor not found"):
        calibrate(kvm, decoder=lambda data: [[BG] * W for _ in range(H)])


def test_implausible_scale_refused():
    # scale 0.45 is just outside the 0.5-2.0 sanity band while keeping every
    # observed grid point clear of the park-corner exclusion zone.
    with pytest.raises(CalibrationError, match="implausible"):
        calibrate(FakeScreenKVM(scale=0.45, offset=0.0))


def test_nonlinear_mapping_fails_verification():
    # Pointer acceleration: a quadratic response fits a plausible-looking line
    # through the symmetric grid (slope 0.85), but the held-out verification
    # point must catch the systematic miss (~0.04 of the screen on one axis).
    kvm = FakeScreenKVM(
        transform=lambda c: min(1.0, max(0.0, 0.1 + 0.85 * c * c))
    )
    with pytest.raises(CalibrationError, match="verification missed"):
        calibrate(kvm)


def test_driver_without_hid_or_video_refused():
    with pytest.raises(CalibrationError, match="cannot calibrate"):
        run_calibration(object(), host="x", decoder=decode, sleep=lambda s: None)


def test_fit_axis_rejects_collinear():
    with pytest.raises(CalibrationError, match="collinear"):
        fit_axis([0.5, 0.5, 0.5], [0.1, 0.2, 0.3])


def test_locate_change_excludes_park_blob():
    base = render(*PARK)
    frame = render(0.75, 0.75)
    # Diff contains the vanished park blob AND the new cursor; the park one
    # must be excluded so the observed position is the new cursor.
    found = locate_change(base, frame, exclude=PARK)
    assert found is not None
    assert found[0] == pytest.approx(0.75, abs=0.02)
    assert found[1] == pytest.approx(0.75, abs=0.02)


def test_persistence_roundtrip_and_resolution_staleness(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cal = MouseCalibration(
        host="h1", resolution="1920x1080", scale_x=0.9, offset_x=0.03,
        scale_y=0.9, offset_y=0.03, residual=0.004, verified_at=1.0,
    )
    save_calibration(cal)
    assert load_calibration("h1", "1920x1080") == cal
    # A different negotiated mode makes the correction stale — never applied.
    assert load_calibration("h1", "1024x768") is None
    assert load_calibration("nope") is None


def test_maybe_apply_reports_honestly(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    kvm = FakeScreenKVM()  # no video_signal_info -> resolution "unknown"
    # No stored calibration: input passes through, calibrated=False.
    assert maybe_apply("h2", kvm, 0.5, 0.5) == (0.5, 0.5, False)
    save_calibration(
        MouseCalibration(
            host="h2", resolution="unknown", scale_x=0.8, offset_x=0.1,
            scale_y=0.8, offset_y=0.1, residual=0.005, verified_at=1.0,
        )
    )
    x, y, calibrated = maybe_apply("h2", kvm, 0.5, 0.5)
    assert calibrated is True
    assert x == pytest.approx((0.5 - 0.1) / 0.8)


def test_maybe_apply_unknown_current_mode_applies_best_effort(tmp_path, monkeypatch):
    """Live .20 lesson: on-demand streamers make the mode unreadable between
    invocations — absence of evidence must not starve the correction. Only an
    *observed* different mode refuses."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    save_calibration(
        MouseCalibration(
            host="h3", resolution="1600x900", scale_x=1.0, offset_x=0.01,
            scale_y=1.0, offset_y=0.01, residual=0.005, verified_at=1.0,
        )
    )
    blind = FakeScreenKVM()  # cannot report a resolution
    assert maybe_apply("h3", blind, 0.5, 0.5)[2] is True

    class SeeingKVM(FakeScreenKVM):
        def __init__(self, w, h):
            super().__init__()
            self._res = (w, h)

        def video_signal_info(self):
            return {"width": self._res[0], "height": self._res[1]}

    assert maybe_apply("h3", SeeingKVM(1600, 900), 0.5, 0.5)[2] is True
    # An observed mode change is real staleness — refuse.
    assert maybe_apply("h3", SeeingKVM(1024, 768), 0.5, 0.5) == (0.5, 0.5, False)
