"""Vision dataset evaluator tests; the backend is always stubbed (issue #292)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "evaluate_dataset", Path(__file__).parents[1] / "tools" / "dataset" / "evaluate.py"
)
assert _SPEC and _SPEC.loader
_evaluate = importlib.util.module_from_spec(_SPEC)
sys.modules["evaluate_dataset"] = _evaluate
_SPEC.loader.exec_module(_evaluate)


def _manifest(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    frames = tmp_path / "frames"
    frames.mkdir()
    for name in {str(row["image"]) for row in rows}:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"stub image bytes")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return manifest


def test_manifest_resolves_relative_images_and_obeys_limit(tmp_path):
    manifest = _manifest(
        tmp_path,
        [
            {"image": "frames/one.jpg", "phase": "booting"},
            {"image": "frames/two.jpg", "phase": "desktop"},
        ],
    )

    entries = _evaluate.load_manifest(manifest, limit=1)

    assert len(entries) == 1
    assert entries[0]["_image_path"] == tmp_path / "frames/one.jpg"
    assert entries[0]["phase"] == "booting"


def test_manifest_rejects_unknown_phase_and_missing_image(tmp_path):
    manifest = _manifest(tmp_path, [{"image": "frames/one.jpg", "phase": "not-a-phase"}])
    with pytest.raises(ValueError, match="unknown phase"):
        _evaluate.load_manifest(manifest)

    manifest.write_text(json.dumps({"image": "missing.jpg", "phase": "booting"}) + "\n")
    with pytest.raises(ValueError, match="does not exist"):
        _evaluate.load_manifest(manifest)


def test_summary_reports_accuracy_confusion_and_degradation_buckets():
    rows = [
        {"expected": "booting", "predicted": "booting", "correct": True, "degradation": "low"},
        {"expected": "booting", "predicted": "desktop", "correct": False, "degradation": "medium"},
        {"expected": "desktop", "predicted": "desktop", "correct": True, "degradation": "unknown"},
    ]

    summary = _evaluate.summarize_results(rows)

    assert summary["accuracy"] == pytest.approx(2 / 3)
    assert summary["per_phase"]["booting"] == {
        "total": 2,
        "correct": 1,
        "accuracy": pytest.approx(0.5),
    }
    assert summary["confusion_matrix"]["booting"]["desktop"] == 1
    assert summary["confusion_matrix"]["desktop"]["desktop"] == 1
    assert summary["by_degradation"]["low"]["accuracy"] == 1.0
    assert summary["by_degradation"]["medium"]["accuracy"] == 0.0


def test_dry_run_never_constructs_or_calls_backend(tmp_path, monkeypatch, capsys):
    manifest = _manifest(tmp_path, [{"image": "frames/one.jpg", "phase": "booting"}])
    make_backend = Mock(side_effect=AssertionError("dry run must not create a backend"))
    monkeypatch.setattr(_evaluate, "make_backend", make_backend)

    assert _evaluate.main([str(manifest), "--backend", "anthropic", "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["rows"] == 1
    assert output["api_calls"] == 0
    make_backend.assert_not_called()


def test_evaluate_passes_base64_image_and_keeps_confidence(tmp_path):
    manifest = _manifest(
        tmp_path,
        [{"image": "frames/one.jpg", "phase": "booting", "capture_params": {"jpeg_quality": 35}}],
    )
    entries = _evaluate.load_manifest(manifest)
    backend = Mock()
    backend.classify.return_value = SimpleNamespace(phase="booting", confidence=0.9)

    summary, results = _evaluate.evaluate_entries(entries, backend)

    assert summary["accuracy"] == 1.0
    assert results[0]["confidence"] == 0.9
    assert results[0]["degradation"] == "low"
    backend.classify.assert_called_once()
    assert backend.classify.call_args.args[0]



def test_evaluate_normalizes_non_finite_quality_for_jsonl(tmp_path):
    manifest = _manifest(
        tmp_path,
        [{"image": "frames/one.jpg", "phase": "booting", "capture_params": {"jpeg_quality": float("nan")}}],
    )
    entries = _evaluate.load_manifest(manifest)
    backend = Mock()
    backend.classify.return_value = SimpleNamespace(phase="booting", confidence=0.9)

    _, results = _evaluate.evaluate_entries(entries, backend)

    assert results[0]["jpeg_quality"] is None
    output = tmp_path / "results.jsonl"
    _evaluate._write_results(results, output)
    json.loads(output.read_text(encoding="utf-8"), parse_constant=lambda value: pytest.fail(value))


def test_main_reports_vision_error_without_traceback(tmp_path, monkeypatch, capsys):
    manifest = _manifest(tmp_path, [{"image": "frames/one.jpg", "phase": "booting"}])
    monkeypatch.setattr(
        _evaluate,
        "make_backend",
        Mock(side_effect=_evaluate.VisionError("backend failed")),
    )

    assert _evaluate.main([str(manifest)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "evaluation failed: backend failed\n"
