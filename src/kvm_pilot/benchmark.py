"""Per-command latency + capability profiler — the data the interface router eats.

The adaptive interface router (#181) picks, for each intent on each device, the
**fastest interface that will actually produce the result right now**. To do that
it needs a *scorecard*: for every command, is this interface capable on this
device, and how fast is it? This module produces that scorecard for the
**library-direct** interface (the engine's own in-process driver calls — the
latency floor). MCP / browser rows are layered on by the router later; the
schema already carries an ``interface`` field so they slot in without a rewrite.

Two axes matter, and both are measured here because "fastest" is meaningless
without "that works":

* **capability** — did the call return without error? This is *state-dependent*,
  not static: on a GL-RM1PE the same ``snapshot`` yields a JPEG at 1024x768 or a
  warm streamer and an undecodable H.264 NAL at 2560x1440 (see the
  ``snapshot-needs-video-client`` quirk, #107/#151). So the scorecard is a
  snapshot-in-time the router must re-take when device/host state changes.
* **latency** — the warm p50, in milliseconds (the first, cold sample is dropped
  so a new-connection TLS handshake doesn't skew it).

stdlib-only; drivers are passed in already built (this module never opens a
connection or chooses credentials).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .drivers.base import Capability

if TYPE_CHECKING:  # avoid importing driver internals at module import time
    from .drivers.base import KVMDriver

# The library-direct interface this module measures. Named so scorecards from
# here compose with the router's MCP/browser rows under one schema.
INTERFACE = "library"


@dataclass(frozen=True)
class Op:
    """One benchmarkable command: the capability it needs and how to invoke it.

    ``capability=None`` marks a structural/offline op (``capabilities``) that
    every driver serves without a network call. ``run`` must be **non-destructive**
    — reads, or the harmless absolute ``mouse_move`` used as the HID probe.
    """

    name: str
    capability: Capability | None
    run: Callable[[KVMDriver], object]
    is_hid: bool = False


# Absolute mid-screen in kvmd raw coordinate space (-32768..32767 → 16000 ≈ 74%).
# A move (never a click) is the safe HID probe: it perturbs nothing on the target.
_HID_PROBE = (16000, 16000)

DEFAULT_OPS: tuple[Op, ...] = (
    Op("capabilities", None, lambda d: d.capabilities()),
    Op("get_info", Capability.SYSTEM_INFO, lambda d: d.get_info()),  # type: ignore[attr-defined]
    Op("get_logs", Capability.LOGS, lambda d: d.get_logs(seek=0)),  # type: ignore[attr-defined]
    Op("is_powered_on", Capability.POWER, lambda d: d.is_powered_on()),  # type: ignore[attr-defined]
    Op("read_sensors", Capability.SENSORS, lambda d: d.read_sensors()),  # type: ignore[attr-defined]
    Op("boot_progress", Capability.BOOT_PROGRESS, lambda d: d.get_boot_progress()),  # type: ignore[attr-defined]
    Op("snapshot", Capability.VIDEO, lambda d: d.snapshot()),  # type: ignore[attr-defined]
    Op("mouse_move", Capability.HID, lambda d: d.mouse_move(*_HID_PROBE), is_hid=True),  # type: ignore[attr-defined]
)


@dataclass
class CommandResult:
    """How one command fared on one interface for one device, right now."""

    command: str
    interface: str
    capable: bool
    p50_ms: float | None
    samples: int
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "interface": self.interface,
            "capable": self.capable,
            "p50_ms": round(self.p50_ms, 1) if self.p50_ms is not None else None,
            "samples": self.samples,
            "note": self.note,
        }


@dataclass
class Scorecard:
    """A device's per-command profile at one point in time.

    ``fastest(command)`` is the router's core query: the cheapest *capable* row.
    Because capability is state-dependent, a scorecard is only valid until the
    device/host state changes (firmware, resolution, streamer warmth, power) —
    the router re-benchmarks on those signals (#181).
    """

    host: str
    driver: str
    firmware: str | None
    results: list[CommandResult] = field(default_factory=list)

    def fastest(self, command: str) -> CommandResult | None:
        """The lowest-latency *capable* result for ``command`` (None if none)."""
        candidates = [
            r for r in self.results
            if r.command == command and r.capable and r.p50_ms is not None
        ]
        return min(candidates, key=lambda r: r.p50_ms) if candidates else None  # type: ignore[arg-type,return-value]

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "driver": self.driver,
            "firmware": self.firmware,
            "results": [r.to_dict() for r in self.results],
        }


def _measure(op: Op, driver: KVMDriver, samples: int, clock: Callable[[], float]) -> CommandResult:
    """Time ``op`` ``samples`` times; drop the cold first sample from the p50.

    A call that raises is caught: the op is marked not-capable and the exception
    type recorded (this is how a state-gated failure — e.g. a snapshot that comes
    back H.264, or a 503 from a cold on-demand streamer — is captured honestly
    rather than crashing the sweep).
    """
    durations: list[float] = []
    ok = 0
    last_err = ""
    for _ in range(max(1, samples)):
        start = clock()
        try:
            op.run(driver)
            durations.append((clock() - start) * 1000.0)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - a failed probe is data, not a crash
            last_err = type(exc).__name__
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            if msg:
                last_err = f"{last_err}: {msg[:80]}"
    warm = durations[1:] if len(durations) > 1 else durations
    p50 = statistics.median(warm) if warm else None
    capable = ok > 0
    return CommandResult(
        command=op.name,
        interface=INTERFACE,
        capable=capable,
        p50_ms=p50,
        samples=ok,
        note="" if capable else (last_err or "no successful sample"),
    )


def benchmark_driver(
    driver: KVMDriver,
    *,
    host: str,
    driver_kind: str,
    firmware: str | None = None,
    samples: int = 6,
    hid: bool = True,
    ops: tuple[Op, ...] = DEFAULT_OPS,
    clock: Callable[[], float] = time.perf_counter,
) -> Scorecard:
    """Profile every op the driver *structurally* supports, in-process.

    Ops whose capability the driver lacks are recorded as ``capable=False`` with
    ``samples=0`` and no network call (so a capability-partial driver — e.g. a
    Redfish BMC with no video — is charted, not skipped silently). Set
    ``hid=False`` to omit the harmless HID mouse-move probe (it moves the target
    cursor). ``clock`` is injectable for deterministic tests.
    """
    results: list[CommandResult] = []
    for op in ops:
        if op.is_hid and not hid:
            continue
        if op.capability is not None and not driver.supports(op.capability):
            results.append(
                CommandResult(
                    command=op.name,
                    interface=INTERFACE,
                    capable=False,
                    p50_ms=None,
                    samples=0,
                    note=f"{op.capability.value} capability not supported by driver",
                )
            )
            continue
        results.append(_measure(op, driver, samples, clock))
    return Scorecard(host=host, driver=driver_kind, firmware=firmware, results=results)
