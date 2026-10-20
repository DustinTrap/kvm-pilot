"""Evaluate a vision backend against a labeled dataset manifest.

This repository-only tool is not included in the installed kvm-pilot package.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from pathlib import Path

from kvm_pilot.errors import VisionError
from kvm_pilot.vision import ALL_PHASES, VisionBackend, make_backend


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a vision backend against a labeled JSONL manifest."
    )
    parser.add_argument("manifest", type=Path, help="path to manifest.jsonl")
    parser.add_argument(
        "--backend",
        default="anthropic",
        help="vision backend kind (anthropic or local/openai-compatible)",
    )
    parser.add_argument("--model", help="model id; required for local backends")
    parser.add_argument("--base-url", help="base URL; required for local backends")
    parser.add_argument("--limit", type=_positive_int, help="evaluate at most N rows")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate selected manifest rows and report the plan without calling a backend",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="optional JSONL path for per-image predictions (contains paths and labels, not image data)",
    )
    return parser


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def load_manifest(manifest: Path, limit: int | None = None) -> list[dict[str, object]]:
    """Read selected rows and resolve each image path relative to the manifest."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive integer")
    if not manifest.is_file():
        raise ValueError(f"manifest does not exist or is not a file: {manifest}")

    entries: list[dict[str, object]] = []
    with manifest.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on manifest line {line_number}: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"manifest line {line_number} must be a JSON object")

            image = row.get("image")
            phase = row.get("phase")
            if not isinstance(image, str) or not image.strip():
                raise ValueError(f"manifest line {line_number} needs a non-empty image path")
            if Path(image).is_absolute():
                raise ValueError(f"manifest line {line_number} image path must be relative")
            if not isinstance(phase, str) or phase not in ALL_PHASES:
                raise ValueError(f"manifest line {line_number} has unknown phase: {phase!r}")

            image_path = manifest.parent / image
            if not image_path.is_file():
                raise ValueError(f"image from manifest line {line_number} does not exist: {image_path}")
            entries.append({**row, "_image_path": image_path})
            if limit is not None and len(entries) >= limit:
                break
    if not entries:
        raise ValueError(f"manifest contains no rows: {manifest}")
    return entries


def degradation_bucket(row: dict[str, object]) -> str:
    """Group augmented rows by JPEG quality; unaugmented rows are unknown."""
    params = row.get("capture_params")
    quality = params.get("jpeg_quality") if isinstance(params, dict) else None
    if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not math.isfinite(quality):
        return "unknown"
    if quality <= 40:
        return "low"
    if quality <= 70:
        return "medium"
    return "high"


def summarize_results(results: list[dict[str, object]]) -> dict[str, object]:
    """Compute accuracy, expected-by-predicted confusion counts, and quality buckets."""
    labels = [phase for phase in ALL_PHASES if any(
        row["expected"] == phase or row["predicted"] == phase for row in results
    )]
    confusion = {expected: {predicted: 0 for predicted in labels} for expected in labels}
    per_phase_counts = {phase: {"total": 0, "correct": 0} for phase in labels}
    bucket_counts = {name: {"total": 0, "correct": 0} for name in ("low", "medium", "high", "unknown")}

    for row in results:
        expected = str(row["expected"])
        predicted = str(row["predicted"])
        correct = expected == predicted
        confusion[expected][predicted] += 1
        per_phase_counts[expected]["total"] += 1
        per_phase_counts[expected]["correct"] += int(correct)
        bucket = str(row["degradation"])
        bucket_counts[bucket]["total"] += 1
        bucket_counts[bucket]["correct"] += int(correct)

    def accuracy(counts: dict[str, int]) -> float:
        return counts["correct"] / counts["total"] if counts["total"] else 0.0

    per_phase = {
        phase: {**counts, "accuracy": accuracy(counts)}
        for phase, counts in per_phase_counts.items()
    }
    by_degradation = {
        bucket: {**counts, "accuracy": accuracy(counts)}
        for bucket, counts in bucket_counts.items()
        if counts["total"]
    }
    total = len(results)
    correct = sum(int(row["correct"]) for row in results)
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "labels": labels,
        "per_phase": per_phase,
        "confusion_matrix": confusion,
        "by_degradation": by_degradation,
    }


def evaluate_entries(
    entries: list[dict[str, object]], backend: VisionBackend
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Classify each selected image and return aggregate metrics and per-row results."""
    results: list[dict[str, object]] = []
    for index, entry in enumerate(entries, start=1):
        image_path = entry["_image_path"]
        if not isinstance(image_path, Path):
            raise ValueError(f"manifest row {index} has no resolved image path")
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        state = backend.classify(image_b64)
        quality = entry.get("capture_params")
        quality = quality.get("jpeg_quality") if isinstance(quality, dict) else None
        if (
            isinstance(quality, bool)
            or not isinstance(quality, (int, float))
            or not math.isfinite(quality)
        ):
            quality = None
        expected = str(entry["phase"])
        results.append(
            {
                "image": str(entry["image"]),
                "expected": expected,
                "predicted": state.phase,
                "correct": state.phase == expected,
                "confidence": state.confidence,
                "jpeg_quality": quality,
                "degradation": degradation_bucket(entry),
            }
        )
    return summarize_results(results), results


def _write_results(results: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in results:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        entries = load_manifest(args.manifest, args.limit)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "backend": args.backend,
                        "rows": len(entries),
                        "limit": args.limit,
                        "api_calls": 0,
                    },
                    indent=2,
                )
            )
            return 0

        backend = make_backend(
            args.backend,
            model=args.model,
            base_url=args.base_url,
        )
        summary, results = evaluate_entries(entries, backend)
        if args.results:
            _write_results(results, args.results)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, VisionError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
