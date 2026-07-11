"""
Firmware registry: load / validate / ingest (issue #80 follow-up).

The registry (``data/firmware_registry.json``) is the single source of truth for
firmware **currency** (latest release, known-bad ranges) and a device's
**capability / UX profile** (the non-live-detectable differentiators: mouse mode,
virtual-media fidelity, power-reading trust, video ceiling), keyed by
``(vendor, product)``. A device is identified by the ``{vendor, product,
version}`` its driver's ``get_firmware_info()`` normalizes, so one generic
mechanism serves every family (PiKVM/GLKVM/Redfish/iDRAC/iLO/…). See
``docs/firmware-registry.md``.

This module is **stdlib-only** and is the shared core for three consumers:
  * ``health.check_firmware_currency`` / ``check_capability_profile`` read it;
  * the ``firmware-report`` GitHub Action runs ``main()`` to fold a submitted
    issue into the registry and open a PR;
  * the tests exercise ``parse_issue_form`` / ``merge_submission`` /
    ``validate_registry`` directly.

A submission is one of three kinds (Latest known release / Known-bad firmware /
Capability profile). Profiles merge **field by field**, so an operator can file an
initial profile on first contact and enrich it later (e.g. add ``vmedia`` once
they've actually booted an ISO) without re-supplying the rest.

Validation is hand-rolled (no ``jsonschema`` dependency) but mirrors
``data/firmware_registry.schema.json`` — keep the two in sync.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Where an opt-in refresh pulls the latest registry from (the repo's raw file on
# GitHub's CDN — free, versioned, single source of truth). Overridable via
# KVM_PILOT_FIRMWARE_DB_URL. A refresh writes to the user cache below; the loader
# then prefers it over the bundled copy. The core never fetches automatically.
DEFAULT_DB_URL = (
    "https://raw.githubusercontent.com/DustinTrap/kvm-pilot/main/"
    "src/kvm_pilot/data/firmware_registry.json"
)
# Where auto-filed firmware reports go (the registry SSoT lives upstream; a
# pip-installed third party has no git remote to derive this from). The
# `firmware-check` CLI accepts --repo to override, e.g. for a fork.
UPSTREAM_REPO = "DustinTrap/kvm-pilot"

SEVERITIES = {"warning", "critical"}
MOUSE_MODES = {"absolute", "relative", "none"}
# Ordered lowest -> highest; a tuple so kvm_pilot.maturity can .index() it (#98).
MATURITY_LEVELS = ("alpha", "beta", "rc", "ga")
VMEDIA_FIDELITY = {"reliable", "reports-only", "none"}
RISK_LEVELS = {"low", "medium", "high"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_RE = re.compile(r"^https?://")
_AFFECTED_RE = re.compile(r"^(<=|>=|==|<|>)?\s*\d+(?:[.\-]\d+)*$")

# Issue-form field labels -> internal submission keys. Keep in lockstep with
# .github/ISSUE_TEMPLATE/firmware-report.yml.
_FIELD_LABELS = {
    "Vendor": "vendor",
    "Product": "product",
    "Submission type": "kind",
    "Latest version (latest-known only)": "latest",
    "Release date (latest-known only, YYYY-MM-DD)": "date",
    "Affected versions (known-bad only)": "affected",
    "Severity (known-bad only)": "severity",
    "Issue / notes (known-bad only)": "issue",
    "Fixed in (optional, known-bad)": "fixed_in",
    "Mouse mode (profile only)": "mouse",
    "Virtual-media fidelity (profile only)": "vmedia",
    "Power-state readings trusted (profile only)": "power_state_trusted",
    "Video ceiling (profile only)": "video",
    "Source URL": "source",
}
_PROFILE_FIELDS = ("mouse", "vmedia", "power_state_trusted", "video")
_NO_RESPONSE = "_no response_"


# --------------------------------------------------------------------------- #
# Version compare (shared with health.check_firmware_currency)                #
# --------------------------------------------------------------------------- #


def _ver_tuple(v: str | None) -> tuple[int, ...]:
    """A version string as its integer segments (``V1.9.1 release1`` -> ``(1, 9, 1, 1)``)."""
    return tuple(int(x) for x in re.findall(r"\d+", v)) if v else ()


def _vercmp(a: str | None, b: str | None) -> int:
    """Compare two dotted-numeric versions: -1 / 0 / 1 (missing segments pad as 0).

    Each numeric segment is compared numerically — correct for kvmd (``4.82``),
    iDRAC (``7.10.30.00``), iLO (``2.78``), and GL (``V1.9.1 release1``). Purely
    non-numeric schemes collapse to () and only ever compare equal.
    """
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def _affected(spec: str, version: str) -> bool:
    """Does ``version`` satisfy a known-bad ``affected`` spec (``<=X`` / ``<X`` /
    ``>=X`` / ``>X`` / ``==X`` / bare ``X`` for exact)?"""
    spec = (spec or "").strip()
    for op in ("<=", ">=", "==", "<", ">"):
        if spec.startswith(op):
            c = _vercmp(version, spec[len(op):].strip())
            return {"<=": c <= 0, "<": c < 0, ">=": c >= 0, ">": c > 0, "==": c == 0}[op]
    return _vercmp(version, spec) == 0


# --------------------------------------------------------------------------- #
# Load                                                                        #
# --------------------------------------------------------------------------- #


def load_registry() -> dict[str, Any]:
    """The active registry, newest valid source first.

    Precedence: ``KVM_PILOT_FIRMWARE_DB`` (an explicit file) > the user cache a
    refresh writes > the copy bundled in the package. A cache/override that is
    missing or fails validation is skipped, so a bad refresh can never take the
    check offline — it just falls back to the bundled data.
    """
    override = os.environ.get("KVM_PILOT_FIRMWARE_DB")
    for path in (Path(override).expanduser() if override else None, cache_path()):
        if path is None:
            continue
        try:
            if path.exists():
                data = json.loads(path.read_text("utf-8"))
                if isinstance(data, dict) and not validate_registry(data):
                    return data
        except (OSError, ValueError):
            pass
    return load_bundled_registry()


def load_bundled_registry() -> dict[str, Any]:
    """The registry that ships inside the installed package (offline default)."""
    try:
        from importlib.resources import files

        raw = (files("kvm_pilot") / "data" / "firmware_registry.json").read_text("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else _empty()
    except (OSError, ValueError, ModuleNotFoundError):
        return _empty()


def cache_path() -> Path:
    """Where an opt-in refresh writes the fetched registry (never the package dir)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "kvm-pilot" / "firmware_registry.json"


def _empty() -> dict[str, Any]:
    return {"schema_version": 2, "updated": "1970-01-01", "firmware": []}


# --------------------------------------------------------------------------- #
# Validate (mirrors firmware_registry.schema.json)                            #
# --------------------------------------------------------------------------- #


def validate_registry(data: Any) -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["registry must be a JSON object"]
    if data.get("schema_version") != 2:
        errs.append("schema_version must be 2")
    if not _DATE_RE.match(str(data.get("updated", ""))):
        errs.append("updated must be YYYY-MM-DD")
    entries = data.get("firmware")
    if not isinstance(entries, list):
        return errs + ["firmware must be an array"]
    for i, e in enumerate(entries):
        errs.extend(f"firmware[{i}]: {m}" for m in _validate_entry(e))
    return errs


def _validate_entry(e: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(e, dict):
        return ["must be an object"]
    if not str(e.get("vendor", "")).strip():
        errs.append("vendor is required")
    if not str(e.get("product", "")).strip():
        errs.append("product is required")
    if "latest" in e:
        if not str(e.get("latest", "")).strip():
            errs.append("latest must be non-empty when present")
        if not _URL_RE.match(str(e.get("source", ""))):
            errs.append("source (http[s] URL) is required with latest")
        if not _DATE_RE.match(str(e.get("date", ""))):
            errs.append("date (YYYY-MM-DD) is required with latest")
    for j, bad in enumerate(e.get("known_bad", []) or []):
        errs.extend(f"known_bad[{j}]: {m}" for m in _validate_known_bad(bad))
    if "profile" in e:
        errs.extend(f"profile: {m}" for m in _validate_profile(e.get("profile")))
    for j, v in enumerate(e.get("versions", []) or []):
        errs.extend(f"versions[{j}]: {m}" for m in _validate_version_row(v))
    return errs


def _validate_known_bad(bad: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(bad, dict):
        return ["must be an object"]
    if not _AFFECTED_RE.match(str(bad.get("affected", "")).strip()):
        errs.append("affected must be a version spec (X, <=X, <X, >=X, >X, ==X)")
    if bad.get("severity") not in SEVERITIES:
        errs.append(f"severity must be one of {sorted(SEVERITIES)}")
    if not str(bad.get("issue", "")).strip():
        errs.append("issue is required")
    if not _URL_RE.match(str(bad.get("source", ""))):
        errs.append("source must be an http(s) URL")
    return errs


def _validate_profile(prof: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(prof, dict):
        return ["must be an object"]
    if "mouse" in prof and prof["mouse"] not in MOUSE_MODES:
        errs.append(f"mouse must be one of {sorted(MOUSE_MODES)}")
    if "vmedia" in prof and prof["vmedia"] not in VMEDIA_FIDELITY:
        errs.append(f"vmedia must be one of {sorted(VMEDIA_FIDELITY)}")
    if "power_state_trusted" in prof and not isinstance(prof["power_state_trusted"], bool):
        errs.append("power_state_trusted must be a boolean")
    if "video" in prof and not str(prof["video"]).strip():
        errs.append("video must be non-empty when present")
    if "remote_update" in prof:
        errs.extend(f"remote_update: {m}" for m in _validate_remote_update(prof["remote_update"]))
    return errs


def _validate_version_row(v: Any) -> list[str]:
    """A per-firmware-version row. ``maturity`` is DERIVED from the run ledger
    by ``kvm_pilot.maturity`` (#98) — never hand-edit it; CI re-derives and
    fails on drift."""
    errs: list[str] = []
    if not isinstance(v, dict):
        return ["must be an object"]
    if not str(v.get("version", "")).strip():
        errs.append("version is required")
    if "maturity" in v:
        m = v["maturity"]
        if not isinstance(m, dict):
            return errs + ["maturity must be an object"]
        if m.get("level") not in MATURITY_LEVELS:
            errs.append(f"maturity.level must be one of {list(MATURITY_LEVELS)}")
        caps = m.get("capabilities", {})
        if not isinstance(caps, dict):
            errs.append("maturity.capabilities must be an object")
        else:
            for name, level in caps.items():
                if not str(name).strip():
                    errs.append("maturity.capabilities keys must be non-empty")
                if level not in MATURITY_LEVELS:
                    errs.append(
                        f"maturity.capabilities[{name!r}] must be one of {list(MATURITY_LEVELS)}"
                    )
    return errs


def _validate_remote_update(ru: Any) -> list[str]:
    """The per-model remote-firmware-update descriptor (health + `firmware-update` read it)."""
    errs: list[str] = []
    if not isinstance(ru, dict):
        return ["must be an object"]
    if "supported" not in ru or not isinstance(ru["supported"], bool):
        errs.append("supported (bool) is required")
    for flag in ("recovery_required", "self_flash_blind"):
        if flag in ru and not isinstance(ru[flag], bool):
            errs.append(f"{flag} must be a boolean")
    if "risk" in ru and ru["risk"] not in RISK_LEVELS:
        errs.append(f"risk must be one of {sorted(RISK_LEVELS)}")
    for text in ("method", "notes"):
        if text in ru and not str(ru[text]).strip():
            errs.append(f"{text} must be non-empty when present")
    return errs


# --------------------------------------------------------------------------- #
# Ingest an issue-form submission                                             #
# --------------------------------------------------------------------------- #


def parse_issue_form(body: str) -> dict[str, str]:
    """Parse a GitHub Issue-Form rendered body (`### Label\\n\\nvalue`) to a dict.

    Unknown headings are ignored; ``_No response_`` (unfilled optional fields)
    becomes an absent key.
    """
    out: dict[str, str] = {}
    parts = re.split(r"^###[ \t]+(.+?)[ \t]*$", body, flags=re.MULTILINE)
    for label, block in zip(parts[1::2], parts[2::2], strict=False):
        key = _FIELD_LABELS.get(label.strip())
        if key is None:
            continue
        value = block.strip()
        if value and value.lower() != _NO_RESPONSE:
            out[key] = value
    return out


def render_issue_form(sub: dict[str, str]) -> str:
    """The inverse of :func:`parse_issue_form`: a submission rendered as the
    Issue-Form body (``### Label\\n\\nvalue`` blocks, in form order).

    Rejects keys that have no form label — that drift would produce a body the
    ingest workflow silently drops fields from.
    """
    unknown = set(sub) - set(_FIELD_LABELS.values())
    if unknown:
        raise ValueError(f"no issue-form field for: {', '.join(sorted(unknown))}")
    return "\n".join(
        f"### {label}\n\n{sub[key]}\n" for label, key in _FIELD_LABELS.items() if key in sub
    )


def _kind(sub: dict[str, str]) -> str:
    k = (sub.get("kind") or "").lower()
    if "known-bad" in k or "known bad" in k:
        return "known_bad"
    if "profile" in k or "capability" in k:
        return "profile"
    return "latest"


def validate_submission(sub: dict[str, str]) -> list[str]:
    """Errors for a parsed submission before it is merged."""
    errs: list[str] = []
    if not sub.get("vendor"):
        errs.append("vendor is required")
    if not sub.get("product"):
        errs.append("product is required")
    kind = _kind(sub)
    src = sub.get("source", "")
    if kind != "profile":  # latest / known-bad must cite a source
        if not _URL_RE.match(src):
            errs.append("source must be an http(s) URL")
    elif src and not _URL_RE.match(src):  # profile source is optional but must be valid if given
        errs.append("source must be an http(s) URL")

    if kind == "known_bad":
        if not _AFFECTED_RE.match((sub.get("affected") or "").strip()):
            errs.append("affected must be a version spec (X, <=X, <X, >=X, >X, ==X)")
        if sub.get("severity") not in SEVERITIES:
            errs.append(f"severity must be one of {sorted(SEVERITIES)}")
        if not sub.get("issue"):
            errs.append("issue is required for a known-bad report")
    elif kind == "profile":
        if not any(sub.get(f) for f in _PROFILE_FIELDS):
            errs.append("a profile report needs at least one of: " + ", ".join(_PROFILE_FIELDS))
        if sub.get("mouse") and sub["mouse"] not in MOUSE_MODES:
            errs.append(f"mouse must be one of {sorted(MOUSE_MODES)}")
        if sub.get("vmedia") and sub["vmedia"] not in VMEDIA_FIDELITY:
            errs.append(f"vmedia must be one of {sorted(VMEDIA_FIDELITY)}")
        if sub.get("power_state_trusted") and sub["power_state_trusted"].lower() not in ("true", "false"):
            errs.append("power_state_trusted must be true or false")
    else:  # latest
        if not sub.get("latest"):
            errs.append("latest version is required for a latest-known report")
        if not _DATE_RE.match(sub.get("date", "")):
            errs.append("date (YYYY-MM-DD) is required for a latest-known report")
    return errs


def _find_entry(registry: dict, vendor: str, product: str) -> dict | None:
    for e in registry.get("firmware", []):
        if e.get("vendor", "").strip().lower() == vendor.strip().lower() and \
                e.get("product", "").strip().lower() == product.strip().lower():
            return e
    return None


def merge_submission(registry: dict, sub: dict[str, str], *, today: str) -> dict:
    """Fold a validated submission into a *copy* of the registry and return it.

    Idempotent; profile reports merge field-by-field so partial enrichment
    accumulates rather than overwriting the whole profile.
    """
    reg = json.loads(json.dumps(registry))  # deep copy; never mutate the input
    reg.setdefault("schema_version", 2)
    reg.setdefault("firmware", [])

    entry = _find_entry(reg, sub["vendor"], sub["product"])
    if entry is None:
        entry = {"vendor": sub["vendor"], "product": sub["product"]}
        reg["firmware"].append(entry)

    kind = _kind(sub)
    if kind == "known_bad":
        bad = {
            "affected": sub["affected"],
            "severity": sub["severity"],
            "issue": sub["issue"],
            "source": sub["source"],
        }
        if sub.get("fixed_in"):
            bad["fixed_in"] = sub["fixed_in"]
        bads = entry.setdefault("known_bad", [])
        for i, b in enumerate(bads):
            if b.get("affected") == bad["affected"]:
                bads[i] = bad
                break
        else:
            bads.append(bad)
    elif kind == "profile":
        prof = entry.setdefault("profile", {})
        for f in ("mouse", "vmedia", "video"):
            if sub.get(f):
                prof[f] = sub[f]
        if sub.get("power_state_trusted"):
            prof["power_state_trusted"] = sub["power_state_trusted"].lower() == "true"
    else:  # latest
        entry["latest"] = sub["latest"]
        entry["source"] = sub["source"]
        entry["date"] = sub["date"]

    reg["updated"] = today
    return reg


def reconcile(vendor: str, product: str, latest: str, *, registry: dict) -> dict | None:
    """Diff a device-reported latest release against the SSoT.

    Returns a ready-to-file "Latest known release" submission when the registry
    has no ``latest`` for ``(vendor, product)`` or its ``latest`` is older than the
    device-reported one; ``None`` when the SSoT already reflects it. This is the
    fleet-feeds-the-registry step: a device that knows its vendor's newest release
    (e.g. GL's ``server_version``) can contribute it upstream.

    A known entry's ``source`` (the vendor release channel, stable across
    versions) is carried into the submission so auto-filing (#189) needs no
    extra input; a device new to the registry has none — the caller must supply
    one, or the submission fails :func:`validate_submission`.
    """
    entry = _find_entry(registry, vendor, product)
    have = (entry or {}).get("latest")
    if have and _vercmp(latest, have) <= 0:
        return None
    sub = {
        "vendor": vendor,
        "product": product,
        "kind": "Latest known release",
        "latest": latest,
    }
    if entry and entry.get("source"):
        sub["source"] = entry["source"]
    return sub


def ingest_batch(registry: dict, items: list[dict], *, today: str) -> tuple[dict, list[dict]]:
    """Fold many issue submissions into one registry pass (the daily batch).

    ``items`` is ``[{"number": int, "body": str}, …]``. Returns the merged
    registry and a per-issue result list (``status`` ∈ ingested / noop / invalid),
    so the workflow can open ONE PR and comment each issue. Idempotent: a report
    already reflected in the registry is a ``noop`` — that dedup is what keeps an
    hourly re-run from churning.
    """
    reg = json.loads(json.dumps(registry))
    results: list[dict] = []
    for item in items:
        num = item.get("number")
        sub = parse_issue_form(item.get("body", ""))
        errs = validate_submission(sub)
        if errs:
            results.append({"number": num, "status": "invalid", "message": "; ".join(errs)})
            continue
        merged = merge_submission(reg, sub, today=today)
        reg_errs = validate_registry(merged)
        if reg_errs:
            results.append({"number": num, "status": "invalid", "message": "; ".join(reg_errs)})
            continue
        if merged.get("firmware") == reg.get("firmware"):
            results.append({"number": num, "status": "noop", "message": "already in the registry"})
        else:
            reg = merged
            results.append({"number": num, "status": "ingested",
                            "message": f"{sub['vendor']} {sub['product']} ({_kind(sub)})"})
    return reg, results


# --------------------------------------------------------------------------- #
# CLI entry point for the GitHub Action                                       #
# --------------------------------------------------------------------------- #


def _write_registry(path: str, registry: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    """Ingest firmware-report issue(s) into a registry file.

    ``--batch <items.json>`` (the hourly workflow) folds every pending issue in
    one pass and writes per-issue outcomes to ``--results``. ``--issue-body-file``
    handles a single issue (manual / tests). ``--today`` supplies the date
    deterministically. Exit codes: 0 changed, 4 no-op, 3 invalid (single only).
    """
    import argparse

    p = argparse.ArgumentParser(description="Ingest firmware-report issue(s) into the registry.")
    p.add_argument("--registry", required=True)
    p.add_argument("--today", required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--issue-body-file", help="one issue body (single-issue mode)")
    src.add_argument("--batch", help="JSON list of {number, body} (batch mode)")
    p.add_argument("--results", help="write per-issue batch outcomes here (JSON)")
    args = p.parse_args(argv)

    with open(args.registry, encoding="utf-8") as fh:
        registry = json.load(fh)

    if args.batch:
        with open(args.batch, encoding="utf-8") as fh:
            items = json.load(fh)
        merged, results = ingest_batch(registry, items, today=args.today)
        if args.results:
            with open(args.results, "w", encoding="utf-8") as fh:
                json.dump(results, fh)
        changed = merged.get("firmware") != registry.get("firmware")
        if changed:
            _write_registry(args.registry, merged)
        counts = {s: sum(1 for r in results if r["status"] == s) for s in ("ingested", "noop", "invalid")}
        print(f"batch: {counts['ingested']} ingested, {counts['noop']} no-op, {counts['invalid']} invalid")
        return 0 if changed else 4

    with open(args.issue_body_file, encoding="utf-8") as fh:
        body = fh.read()
    sub = parse_issue_form(body)
    sub_errs = validate_submission(sub)
    if sub_errs:
        print("INVALID SUBMISSION:", file=sys.stderr)
        for e in sub_errs:
            print(f"  - {e}", file=sys.stderr)
        return 3

    merged = merge_submission(registry, sub, today=args.today)
    reg_errs = validate_registry(merged)
    if reg_errs:
        print("MERGED REGISTRY FAILED VALIDATION:", file=sys.stderr)
        for e in reg_errs:
            print(f"  - {e}", file=sys.stderr)
        return 3

    if merged.get("firmware") == registry.get("firmware"):
        print("no change: entry already present")
        return 4

    _write_registry(args.registry, merged)
    print(f"registry updated: {sub['vendor']} {sub['product']} ({_kind(sub)})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
