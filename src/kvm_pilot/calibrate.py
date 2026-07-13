"""Vision-based mouse auto-calibration (#128).

Absolute mouse positioning through a KVM assumes the target OS puts the cursor
exactly where the commanded kvmd coordinate says. In practice OS acceleration
curves, capture letterboxing, and multi-monitor layouts skew that mapping —
the agent clicks where the button *should* be and misses. This module measures
the real commanded→observed mapping and stores a per-host correction:

1. park the cursor near a corner and snapshot a baseline;
2. command absolute moves to a small grid of known percent-space points,
   snapshotting after each;
3. locate the observed cursor as the changed region vs the baseline
   (largest connected blob, park corner excluded);
4. least-squares fit per-axis scale+offset, then verify a held-out point
   lands within tolerance;
5. persist per (host, capture resolution); the percent→kvmd conversion
   applies the inverse transparently wherever percent coords are used.

The moves are pointer-only (no clicks, no keystrokes) — nothing here is in
``DESTRUCTIVE_OPS``; the MCP tool still gates the run behind the HID effect
class because moving a live console's pointer is agent-visible action.

Pixel access: decoding the JPEG needs Pillow (the ``calibrate`` extra),
imported lazily so the core stays stdlib-only at import time. The detection
and fitting math below is pure stdlib and is unit-tested with synthetic
frames — tests inject a decoder and never touch Pillow.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import KVMPilotError
from .health import _cache_base_dir

# JPEG bytes -> row-major luminance matrix (list of rows of 0-255 ints).
# The default decoder downscales to ~DECODE_WIDTH so a frame is a few
# hundred KB of ints, not tens of MB; centroid quantization at that width
# is well inside the verification tolerance.
Decoder = Callable[[bytes], list[list[int]]]

DECODE_WIDTH = 480
# Calibration grid: center + the four quadrant midpoints. VERIFY_POINT is
# held out of the fit so the check is honest.
GRID = ((0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
VERIFY_POINT = (0.6, 0.4)
PARK = (0.02, 0.02)
# Blobs whose centroid lies within this fraction of the park corner are the
# cursor's *old* position (or corner UI chrome) — never the observed target.
PARK_EXCLUDE_RADIUS = 0.08


class CalibrationError(KVMPilotError):
    """Calibration could not produce a trustworthy correction."""


def default_decoder(data: bytes) -> list[list[int]]:
    """JPEG -> downscaled luminance matrix via Pillow (lazy import).

    Pillow is the ``calibrate`` extra, imported here and nowhere else so the
    library core keeps its stdlib-only-at-import guarantee.
    """
    try:
        from PIL import Image  # noqa: PLC0415 - lazy optional dependency
    except ImportError as exc:
        raise CalibrationError(
            "mouse calibration needs Pillow to decode snapshots — install it with "
            "pip install 'kvm-pilot[calibrate]' (or pass your own decoder=)"
        ) from exc
    import io  # noqa: PLC0415

    img = Image.open(io.BytesIO(data)).convert("L")
    if img.width > DECODE_WIDTH:
        img = img.resize((DECODE_WIDTH, max(1, round(img.height * DECODE_WIDTH / img.width))))
    px = list(img.getdata())
    w = img.width
    return [px[i : i + w] for i in range(0, len(px), w)]


def changed_blobs(
    base: list[list[int]],
    frame: list[list[int]],
    *,
    threshold: int = 40,
    min_pixels: int = 2,
) -> list[tuple[int, float, float]]:
    """All changed blobs between two frames as ``(size, cx, cy)``, largest first.

    4-connected flood fill over ``|frame - base| > threshold``; centroids are
    screen fractions. A diff against the calibration baseline contains up to
    two blobs: where the cursor *went* (the observation) and where it *was* in
    the baseline (the departure mark) — callers separate them.
    """
    h = min(len(base), len(frame))
    if h == 0:
        return []
    w = min(len(base[0]), len(frame[0]))
    changed = [
        [abs(frame[y][x] - base[y][x]) > threshold for x in range(w)] for y in range(h)
    ]
    seen = [[False] * w for _ in range(h)]
    out: list[tuple[int, float, float]] = []
    for y0 in range(h):
        for x0 in range(w):
            if not changed[y0][x0] or seen[y0][x0]:
                continue
            stack, blob = [(x0, y0)], []
            seen[y0][x0] = True
            while stack:
                x, y = stack.pop()
                blob.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and changed[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if len(blob) < min_pixels:
                continue
            cx = sum(p[0] for p in blob) / len(blob) / max(1, w - 1)
            cy = sum(p[1] for p in blob) / len(blob) / max(1, h - 1)
            out.append((len(blob), cx, cy))
    out.sort(reverse=True)
    return out


def locate_change(
    base: list[list[int]],
    frame: list[list[int]],
    *,
    threshold: int = 40,
    min_pixels: int = 2,
    exclude: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """Centroid of the largest changed blob, skipping any near ``exclude``."""
    for _size, cx, cy in changed_blobs(base, frame, threshold=threshold, min_pixels=min_pixels):
        if exclude is not None and (
            abs(cx - exclude[0]) < PARK_EXCLUDE_RADIUS
            and abs(cy - exclude[1]) < PARK_EXCLUDE_RADIUS
        ):
            continue
        return (cx, cy)
    return None


def _stationary_point(
    per_frame: list[list[tuple[int, float, float]]], eps: float = 0.04
) -> tuple[float, float] | None:
    """The centroid that recurs in *every* frame's diff-vs-baseline.

    That recurring blob is the cursor's departure mark — the spot the cursor
    occupied in the baseline shows as changed in every later diff. It must be
    identified from the data, NOT assumed to sit at the commanded park point:
    the host's distortion (the very thing being measured) decides where the
    parked cursor actually landed.
    """
    if not per_frame or not per_frame[0]:
        return None
    for _size, cx, cy in per_frame[0]:
        hits = sum(
            any(abs(bx - cx) < eps and abs(by - cy) < eps for _s, bx, by in blobs)
            for blobs in per_frame
        )
        if hits == len(per_frame):
            return (cx, cy)
    return None


def _observation(
    blobs: list[tuple[int, float, float]],
    stationary: tuple[float, float] | None,
    eps: float = 0.04,
) -> tuple[float, float] | None:
    """Largest blob that isn't the baseline cursor's departure mark."""
    for _size, cx, cy in blobs:
        if stationary is not None and (
            abs(cx - stationary[0]) < eps and abs(cy - stationary[1]) < eps
        ):
            continue
        return (cx, cy)
    return None


def fit_axis(commanded: list[float], observed: list[float]) -> tuple[float, float]:
    """Least-squares ``observed = scale * commanded + offset`` for one axis."""
    n = len(commanded)
    mean_c = sum(commanded) / n
    mean_o = sum(observed) / n
    var = sum((c - mean_c) ** 2 for c in commanded)
    if var == 0:
        raise CalibrationError("calibration points are collinear on one axis")
    scale = (
        sum((c - mean_c) * (o - mean_o) for c, o in zip(commanded, observed, strict=True)) / var
    )
    return scale, mean_o - scale * mean_c


@dataclass(frozen=True)
class MouseCalibration:
    """Per-(host, resolution) correction: ``observed = scale * commanded + offset``."""

    host: str
    resolution: str  # "1920x1080", or "unknown" when the driver can't report it
    scale_x: float
    offset_x: float
    scale_y: float
    offset_y: float
    residual: float  # worst held-out verification error, screen fraction
    verified_at: float  # epoch seconds

    def apply(self, x: float, y: float) -> tuple[float, float]:
        """Percent coords the *agent wants* -> percent coords to *command*.

        Inverts the fitted mapping and clamps to the screen; scales are
        sanity-bounded at fit time so the division is safe.
        """
        cx = (x - self.offset_x) / self.scale_x
        cy = (y - self.offset_y) / self.scale_y
        return (min(1.0, max(0.0, cx)), min(1.0, max(0.0, cy)))

    def to_dict(self) -> dict:
        return asdict(self)


def _store_path() -> Path:
    return Path(_cache_base_dir()) / "kvm-pilot" / "mouse_calibration.json"


def _load_store() -> dict:
    try:
        return json.loads(_store_path().read_text())
    except (OSError, ValueError):
        return {}


def save_calibration(cal: MouseCalibration) -> Path:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = _load_store()
    store[cal.host] = cal.to_dict()
    path.write_text(json.dumps(store, indent=2, sort_keys=True))
    return path


def load_calibration(host: str, resolution: str | None = None) -> MouseCalibration | None:
    """Stored correction for ``host``, or None.

    A stored row for a *different* resolution is stale (the skew is a function
    of the negotiated mode) and is treated as absent, never applied.
    """
    row = _load_store().get(host)
    if not isinstance(row, dict):
        return None
    try:
        cal = MouseCalibration(**row)
    except TypeError:
        return None
    if resolution is not None and cal.resolution not in ("unknown", resolution):
        return None
    return cal


def current_resolution(kvm) -> str:
    """Capture resolution as "WxH", or "unknown" when unreported (fake, no signal)."""
    signal_state = getattr(kvm, "signal_state", None)
    if signal_state is None:
        return "unknown"
    try:
        sig = signal_state()
    except KVMPilotError:
        return "unknown"
    w, h = sig.get("width"), sig.get("height")
    return f"{w}x{h}" if w and h else "unknown"


def maybe_apply(host: str, kvm, x: float, y: float) -> tuple[float, float, bool]:
    """Apply a stored, resolution-matching correction to percent coords.

    Returns ``(x, y, calibrated)`` — the untouched input with ``False`` when no
    usable calibration exists, so callers can report honestly (#141 doctrine).
    The resolution probe runs only when a stored row exists at all.
    """
    if load_calibration(host) is None:
        return (x, y, False)
    cal = load_calibration(host, current_resolution(kvm))
    if cal is None:
        return (x, y, False)
    cx, cy = cal.apply(x, y)
    return (cx, cy, True)


def run_calibration(
    kvm,
    *,
    host: str,
    decoder: Decoder | None = None,
    settle: float = 0.4,
    threshold: int = 40,
    tolerance: float = 0.02,
    sleep: Callable[[float], None] = time.sleep,
) -> MouseCalibration:
    """Measure and verify the commanded→observed mouse mapping. Moves only.

    Preconditions the caller should ensure (and this function checks where it
    can): a live video signal, a *static* screen (no video playing, no
    animations under the grid points), and a visible cursor. Raises
    :class:`CalibrationError` with an actionable reason otherwise. The cursor
    is parked back at the corner on success.
    """
    decode = decoder or default_decoder
    move = getattr(kvm, "mouse_move_percent", None)
    snap = getattr(kvm, "snapshot", None)
    if move is None or snap is None:
        raise CalibrationError(
            "this driver cannot calibrate: it needs absolute percent mouse moves "
            "and snapshots (PiKVM-family capture devices have both; BMCs have neither)"
        )

    def observe() -> list[list[int]]:
        sleep(settle)
        return decode(snap())

    move(*PARK)
    base = observe()
    # Static-screen gate: with the cursor parked, two consecutive frames must
    # not differ — a video/animation would masquerade as the cursor blob.
    drift = locate_change(base, observe(), threshold=threshold)
    if drift is not None:
        raise CalibrationError(
            "the screen is changing on its own (video/animation at "
            f"~({drift[0]:.2f}, {drift[1]:.2f})) — calibrate on a static screen "
            "(a desktop, BIOS menu, or login prompt)"
        )

    # Each diff-vs-baseline holds the arrival blob (the observation) and the
    # departure mark where the cursor sat in the baseline. The departure mark
    # is wherever the *distorted* park landed — identified as the blob that
    # recurs across every frame, never assumed from the commanded corner.
    per_frame = []
    for tx, ty in GRID:
        move(tx, ty)
        per_frame.append(changed_blobs(base, observe(), threshold=threshold))
    stationary = _stationary_point(per_frame)

    commanded_x: list[float] = []
    commanded_y: list[float] = []
    observed_x: list[float] = []
    observed_y: list[float] = []
    for (tx, ty), blobs in zip(GRID, per_frame, strict=True):
        found = _observation(blobs, stationary)
        if found is None:
            raise CalibrationError(
                f"cursor not found after moving to ({tx}, {ty}) — make sure the "
                "cursor is visible (not hidden by the OS), the screen is static, "
                "and there is a live video signal"
            )
        commanded_x.append(tx)
        commanded_y.append(ty)
        observed_x.append(found[0])
        observed_y.append(found[1])

    scale_x, offset_x = fit_axis(commanded_x, observed_x)
    scale_y, offset_y = fit_axis(commanded_y, observed_y)
    for axis, scale in (("x", scale_x), ("y", scale_y)):
        if not 0.5 <= abs(scale) <= 2.0:
            raise CalibrationError(
                f"implausible {axis}-axis scale {scale:.2f} — the located blob is "
                "probably not the cursor (screen changed mid-run?); retry on a "
                "static screen"
            )

    cal = MouseCalibration(
        host=host,
        resolution=current_resolution(kvm),
        scale_x=scale_x,
        offset_x=offset_x,
        scale_y=scale_y,
        offset_y=offset_y,
        residual=0.0,
        verified_at=time.time(),
    )
    # Held-out verification: command through the inverse, demand the observed
    # cursor lands on the *desired* point within tolerance.
    want = VERIFY_POINT
    move(*cal.apply(*want))
    seen = _observation(changed_blobs(base, observe(), threshold=threshold), stationary)
    if seen is None:
        raise CalibrationError("cursor not found during verification — retry on a static screen")
    residual = max(abs(seen[0] - want[0]), abs(seen[1] - want[1]))
    if residual > tolerance:
        raise CalibrationError(
            f"verification missed by {residual:.3f} of the screen "
            f"(tolerance {tolerance}) — the mapping may not be linear on this "
            "host (OS pointer acceleration?); disable acceleration on the "
            "target or raise tolerance"
        )
    move(*PARK)
    return MouseCalibration(**{**cal.to_dict(), "residual": residual})
