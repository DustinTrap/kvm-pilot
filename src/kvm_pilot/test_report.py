"""
Live-test harness: probe a device's capabilities and append the evidence to the
run ledger (issue #99, part of #96).

Each probe asserts **assertion + observed effect** — the #94 lesson: a call that
"succeeds" is not evidence until the result shape (read-only) or a post-condition
(destructive) is observed. Failures are DATA, not harness errors: an honest FAIL
row is exactly what the support matrix wants (docs/test-plan.md §0–§2).

Read-only capabilities run on every invocation. Destructive capabilities run
only when explicitly named (``--include``) with an operator attestation, and
still route through each driver's normal ``safety.guard`` gates — attestation is
a *recording* requirement layered on top of, never a replacement for, the
safety layer. Snapshot rows always carry the #156 ``conditions`` axes.

Stdlib-only (repo rule): no third-party imports at module level.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .__about__ import __version__
from .drivers.base import Capability
from .errors import KVMPilotError, SafetyError
from .support_matrix import DESTRUCTIVE_CAPS

# Ledger capability names (support_matrix.KNOWN_CAPS order) -> the structural
# Capability the driver must support for the probe to apply (None = universal).
READ_ONLY_CAPS: list[str] = ["info", "snapshot", "healthcheck", "logs", "power_state"]
_CAP_REQUIRES: dict[str, Capability | None] = {
    "info": Capability.SYSTEM_INFO,
    "snapshot": Capability.VIDEO,
    "healthcheck": None,
    "logs": Capability.LOGS,
    "power_state": Capability.POWER,
    "power": Capability.POWER,
    "virtual_media": Capability.VIRTUAL_MEDIA,
    "firmware_update": Capability.FIRMWARE_UPDATE,
}


def default_ledger_path() -> Path:
    """Where a run is recorded: ``KVM_PILOT_TEST_LEDGER`` else the user config
    dir (never the installed package data — a pip install's bundled ledger is
    package content, and synthetic rows must not silently land there)."""
    override = os.environ.get("KVM_PILOT_TEST_LEDGER")
    if override:
        return Path(override).expanduser()
    from .config import _config_base_dir  # platform-correct (%APPDATA% on Windows)

    return Path(_config_base_dir()) / "kvm-pilot" / "test_runs.jsonl"


def append_row(path: Path, row: dict[str, Any]) -> Path:
    """Plain append — the ledger is append-only JSONL; readers dedupe by run_id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def make_run_id(source: str, product: str, now: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", product.lower()) or "device"
    return f"{source}-{slug}-{now.strftime('%Y%m%d%H%M%S')}"


def device_identity(kvm: Any) -> dict[str, str]:
    """Vendor/product/firmware from the driver's own normalized path
    (``get_firmware_info`` — the same identity the registry/matrix joins on,
    per maturity.py's dedupe warning). Honest fallback for the fake driver."""
    fn = getattr(kvm, "get_firmware_info", None)
    fw: dict[str, Any] = {}
    if fn is not None:
        try:
            fw = fn() or {}
        except KVMPilotError:
            fw = {}
    return {
        "vendor": (fw.get("vendor") or "fake").strip(),
        "product": fw.get("product") or fw.get("model") or "fake",
        "firmware_version": fw.get("version") or "none",
    }


def snapshot_conditions(kvm: Any) -> dict[str, Any] | None:
    """The #156 axes a snapshot outcome depends on, gathered best-effort.

    ``resolution``/``encoder_format`` come from ``video_signal_info``;
    ``jpeg_sink_clients``/``snapshot_cached`` from the raw streamer state when
    the driver exposes it. Unobservable axes are OMITTED, never fabricated.
    """
    cond: dict[str, Any] = {}
    vsi_fn = getattr(kvm, "video_signal_info", None)
    if vsi_fn is not None:
        try:
            vsi = vsi_fn() or {}
        except KVMPilotError:
            vsi = {}
        if vsi.get("width") and vsi.get("height"):
            cond["resolution"] = f"{vsi['width']}x{vsi['height']}"
        if vsi.get("format"):
            cond["encoder_format"] = str(vsi["format"]).lower()
    state_fn = getattr(kvm, "get_streamer_state", None)
    if state_fn is not None:
        try:
            state = state_fn() or {}
        except KVMPilotError:
            state = {}

        def _as_dict(val: object) -> dict[str, Any]:
            return val if isinstance(val, dict) else {}

        streamer = _as_dict(_as_dict(state).get("streamer"))
        jpeg = _as_dict(_as_dict(streamer.get("sinks")).get("jpeg"))
        if "has_clients" in jpeg:
            cond["jpeg_sink_clients"] = bool(jpeg["has_clients"])
        snap = _as_dict(streamer.get("snapshot"))
        if "saved" in snap:
            cond["snapshot_cached"] = bool(snap["saved"])
    return cond or None


def _row(capability: str, passed: bool, outcome: str,
         conditions: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"capability": capability, "passed": passed,
                           "outcome": outcome[:300]}
    if conditions:
        out["conditions"] = conditions
    return out


def _wait_until(pred: Callable[[], bool], timeout: float = 10.0, poll: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


# -- probes: read-only -------------------------------------------------------


def probe_info(kvm: Any) -> dict[str, Any]:
    try:
        info = kvm.get_info()
    except KVMPilotError as exc:
        return _row("info", False, str(exc))
    if not isinstance(info, dict) or not info:
        return _row("info", False, f"empty/malformed info: {type(info).__name__}")
    return _row("info", True, f"fields: {', '.join(sorted(info)[:6])}")


def probe_snapshot(kvm: Any) -> dict[str, Any]:
    # Conditions are gathered regardless of outcome (#156: pass AND fail rows
    # must carry them — that's what makes two reports reconcile from data).
    cond = snapshot_conditions(kvm)
    try:
        data = kvm.snapshot()
    except KVMPilotError as exc:
        return _row("snapshot", False, str(exc), cond)
    if not data or not data.startswith(b"\xff\xd8\xff"):
        return _row("snapshot", False,
                    f"non-JPEG or empty bytes (len={len(data or b'')})", cond)
    return _row("snapshot", True, f"jpeg {len(data)} bytes", cond)


def probe_healthcheck(kvm: Any) -> dict[str, Any]:
    from .health import run_healthcheck

    try:
        report = run_healthcheck(kvm)
    except KVMPilotError as exc:
        return _row("healthcheck", False, str(exc))
    criticals = ", ".join(r.id for r in report.criticals)
    # A CRITICAL finding is the healthcheck DOING ITS JOB, not a probe failure.
    return _row("healthcheck", True,
                f"worst={report.worst}" + (f" ({criticals})" if criticals else ""))


def probe_logs(kvm: Any) -> dict[str, Any]:
    try:
        text = kvm.get_logs(seek=300)
    except KVMPilotError as exc:
        return _row("logs", False, str(exc))
    if not isinstance(text, str):
        return _row("logs", False, f"non-text log payload: {type(text).__name__}")
    return _row("logs", True, f"text buffer ({len(text)} chars)")


def probe_power_state(kvm: Any) -> dict[str, Any]:
    try:
        value = kvm.is_powered_on()
    except KVMPilotError as exc:
        return _row("power_state", False, str(exc))
    if not isinstance(value, bool):
        return _row("power_state", False, f"non-bool power state: {value!r}")
    return _row("power_state", True, f"bool ({'on' if value else 'off'})")


# -- probes: destructive (opt-in, attested, effect-verified) -----------------


def probe_power(kvm: Any) -> dict[str, Any]:
    """Toggle away from the baseline, observe the read-back flip, restore.

    Pass requires BOTH transitions observed — on hardware whose power sensing
    lies (GL: quirk atx-power-state-always-off) this honestly FAILs, which is
    the finding. The restore attempt always runs.
    """
    baseline = kvm.is_powered_on()
    away = kvm.power_off if baseline else kvm.power_on
    back = kvm.power_on if baseline else kvm.power_off
    try:
        away()
        flipped = _wait_until(lambda: kvm.is_powered_on() != baseline)
    finally:
        try:
            back()
        except KVMPilotError:
            pass
    restored = _wait_until(lambda: kvm.is_powered_on() == baseline)
    if flipped and restored:
        return _row("power", True,
                    f"observed {'on->off->on' if baseline else 'off->on->off'}")
    leg = "toggle" if not flipped else "restore"
    return _row("power", False,
                f"{leg} transition not observed via power read-back "
                f"(baseline powered_on={baseline})")


def probe_virtual_media(kvm: Any, iso: str) -> dict[str, Any]:
    """Mount (drivers verify by default, #77/#169), then observe the eject."""
    try:
        name = kvm.mount_iso(iso)
    except KVMPilotError as exc:
        return _row("virtual_media", False, f"mount: {exc}")
    online = bool((kvm.get_msd_state() or {}).get("online"))
    if not online:
        return _row("virtual_media", False,
                    f"mount of {name!r} accepted but MSD never reported online")
    try:
        kvm.msd_disconnect()
    except KVMPilotError as exc:
        return _row("virtual_media", False, f"eject: {exc}")
    ejected = _wait_until(
        lambda: not bool((kvm.get_msd_state() or {}).get("online")), timeout=5.0
    )
    if not ejected:
        return _row("virtual_media", False,
                    f"mounted {name!r} ok but eject effect not observed (still online)")
    return _row("virtual_media", True, f"mounted {name!r} online, eject observed")


def probe_firmware_update(kvm: Any, image: str | None) -> dict[str, Any]:
    """The driver's own #94 contract: 'sent' only after an observed state change."""
    try:
        result = kvm.apply_firmware_update(image=image, dry_run=False)
    except KVMPilotError as exc:
        return _row("firmware_update", False, str(exc))
    if result.get("sent"):
        return _row("firmware_update", True, f"verified: {result.get('verified')}")
    return _row("firmware_update", False,
                str(result.get("error") or "flash not verified (no observed state change)"))


# -- orchestration ------------------------------------------------------------


def run_probes(
    kvm: Any,
    *,
    include: frozenset[str] = frozenset(),
    iso: str | None = None,
    image: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run the applicable probes; return (capability rows, skipped names).

    Skipped = structurally unsupported, or a destructive probe the operator
    declined at the safety prompt ("never exercised", not a FAIL).
    """
    probes: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("info", lambda: probe_info(kvm)),
        ("snapshot", lambda: probe_snapshot(kvm)),
        ("healthcheck", lambda: probe_healthcheck(kvm)),
        ("logs", lambda: probe_logs(kvm)),
        ("power_state", lambda: probe_power_state(kvm)),
    ]
    if "power" in include:
        probes.append(("power", lambda: probe_power(kvm)))
    if "virtual_media" in include:
        probes.append(("virtual_media", lambda: probe_virtual_media(kvm, iso or "")))
    if "firmware_update" in include:
        probes.append(("firmware_update", lambda: probe_firmware_update(kvm, image)))

    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name, probe in probes:
        required = _CAP_REQUIRES[name]
        if required is not None and not kvm.supports(required):
            skipped.append(f"{name} (capability not supported by this driver)")
            continue
        try:
            rows.append(probe())
        except SafetyError:
            # Operator declined at the prompt: the capability was NOT exercised.
            skipped.append(f"{name} (declined at the safety prompt)")
    return rows, skipped


def build_row(
    kvm: Any,
    caps: list[dict[str, Any]],
    *,
    source: str,
    operator: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    when = now or datetime.now(UTC)
    identity = device_identity(kvm)
    row: dict[str, Any] = {
        "run_id": make_run_id(source, identity["product"], when),
        "source": source,
        **identity,
        "kvm_pilot_version": __version__,
        "driver": type(kvm).__name__.removesuffix("Driver").lower(),
        "os_family": platform.system().lower(),
        "python_version": platform.python_version(),
        "utc_date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capabilities": caps,
    }
    if operator:
        row["operator"] = operator
    return row


__all__ = [
    "READ_ONLY_CAPS",
    "DESTRUCTIVE_CAPS",
    "default_ledger_path",
    "append_row",
    "make_run_id",
    "device_identity",
    "snapshot_conditions",
    "run_probes",
    "build_row",
]
