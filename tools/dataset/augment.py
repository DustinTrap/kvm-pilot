"""Create deterministic KVM-style degradation variants for vision datasets.

This is dataset-build tooling, not part of the installed ``kvm_pilot`` package.
It deliberately uses Pillow, which is already a base project dependency, rather
than adding an image-processing dependency to the runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance
from PIL import __version__ as PILLOW_VERSION

from kvm_pilot.vision.base import ALL_PHASES

AUGMENTATION_VERSION = "mjpeg_kvm_v1"
SPLITS = ("train", "validation", "test")
SOURCE_KINDS = ("synthetic", "real_kvm", "wikimedia", "other")


@dataclass(frozen=True)
class AugmentationParams:
    """Fully materialized parameters for one reproducible output image."""

    seed: int
    scale: float
    jpeg_quality: int
    jpeg_subsampling: int
    gamma: float
    brightness: float
    letterbox_x: int
    letterbox_y: int
    scanline_strength: int
    tear_y: int
    tear_height: int
    tear_offset: int


def sample_params(seed: int, width: int, height: int) -> AugmentationParams:
    """Sample bounded degradation parameters from ``seed``.

    A dedicated RNG per variant means adding more variants later does not alter
    any already-generated image.
    """
    if width < 1 or height < 1:
        raise ValueError("image dimensions must be positive")

    rng = random.Random(seed)
    max_letterbox_x = max(0, min(width // 8, round(width * 0.04)))
    max_letterbox_y = max(0, min(height // 8, round(height * 0.04)))
    max_tear_height = max(0, min(height // 12, 24))
    max_tear_offset = max(0, min(width // 10, 96))
    tear_height = rng.randint(0, max_tear_height)
    tear_y = rng.randint(0, max(0, height - tear_height)) if tear_height else 0

    return AugmentationParams(
        seed=seed,
        scale=round(rng.uniform(0.55, 0.92), 4),
        jpeg_quality=rng.randint(28, 72),
        jpeg_subsampling=rng.choice((0, 1, 2)),
        gamma=round(rng.uniform(0.78, 1.28), 4),
        brightness=round(rng.uniform(0.82, 1.16), 4),
        letterbox_x=rng.randint(0, max_letterbox_x),
        letterbox_y=rng.randint(0, max_letterbox_y),
        scanline_strength=rng.randint(0, 28),
        tear_y=tear_y,
        tear_height=tear_height,
        tear_offset=rng.randint(-max_tear_offset, max_tear_offset),
    )


def _apply_gamma(image: Image.Image, gamma: float) -> Image.Image:
    lut = [min(255, max(0, round(255 * ((value / 255) ** gamma)))) for value in range(256)]
    return image.point(lut * len(image.getbands()))


def _apply_letterbox(image: Image.Image, x_pad: int, y_pad: int) -> Image.Image:
    width, height = image.size
    inner_width = max(1, width - (2 * x_pad))
    inner_height = max(1, height - (2 * y_pad))
    if inner_width == width and inner_height == height:
        return image
    canvas = Image.new("RGB", image.size, "black")
    resized = image.resize((inner_width, inner_height), Image.Resampling.BILINEAR)
    canvas.paste(resized, ((width - inner_width) // 2, (height - inner_height) // 2))
    return canvas


def _apply_scanlines(image: Image.Image, strength: int) -> Image.Image:
    if strength <= 0:
        return image
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    for y in range(1, image.height, 2):
        draw.line((0, y, image.width - 1, y), fill=strength)
    return Image.composite(Image.new("RGB", image.size, "black"), image, mask)


def _apply_tearing(image: Image.Image, *, y: int, height: int, offset: int) -> Image.Image:
    if height <= 0 or offset == 0:
        return image
    bottom = min(image.height, y + height)
    if bottom <= y:
        return image
    output = image.copy()
    band = image.crop((0, y, image.width, bottom))
    output.paste(ImageChops.offset(band, offset, 0), (0, y))
    return output


def augment_image(image: Image.Image, params: AugmentationParams) -> bytes:
    """Apply one parameter set and return encoded JPEG bytes.

    Output dimensions match the source. The lossy JPEG encode is deliberately
    last so every preceding transform is exposed to the same codec artifacts a
    KVM MJPEG stream would add.
    """
    frame = image.convert("RGB")
    width, height = frame.size
    downscaled = (
        max(1, round(width * params.scale)),
        max(1, round(height * params.scale)),
    )
    frame = frame.resize(downscaled, Image.Resampling.BILINEAR)
    frame = frame.resize((width, height), Image.Resampling.BILINEAR)
    frame = ImageEnhance.Brightness(frame).enhance(params.brightness)
    frame = _apply_gamma(frame, params.gamma)
    frame = _apply_letterbox(frame, params.letterbox_x, params.letterbox_y)
    frame = _apply_scanlines(frame, params.scanline_strength)
    frame = _apply_tearing(
        frame,
        y=params.tear_y,
        height=params.tear_height,
        offset=params.tear_offset,
    )

    output = io.BytesIO()
    frame.save(
        output,
        format="JPEG",
        quality=params.jpeg_quality,
        subsampling=params.jpeg_subsampling,
        optimize=False,
    )
    return output.getvalue()


def _open_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            image.load()
            return image.convert("RGB")
    except OSError as exc:
        raise ValueError(f"{path} is not a decodable image: {exc}") from exc


def build_variants(
    input_path: Path,
    output_dir: Path,
    *,
    phase: str,
    count: int,
    seed: int,
    split: str,
    source_kind: str,
    source_reference: str,
    license_id: str,
    attribution: str,
    redistributable: bool,
) -> list[dict]:
    """Write variants and return JSONL-ready manifest entries."""
    if phase not in ALL_PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if count < 1:
        raise ValueError("count must be at least 1")
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {source_kind!r}")
    if not source_reference:
        raise ValueError("source reference must not be empty")
    if not license_id:
        raise ValueError("license id must not be empty")

    image = _open_image(input_path)
    source_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = []

    for index in range(count):
        variant_seed = seed + index
        params = sample_params(variant_seed, *image.size)
        encoded = augment_image(image, params)
        output_path = output_dir / f"{input_path.stem}.mjpeg-{index:04d}.jpg"
        output_path.write_bytes(encoded)
        entries.append(
            {
                "schema_version": 1,
                "image": output_path.name,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "source_sha256": source_sha256,
                "phase": phase,
                "split": split,
                "source": {
                    "kind": source_kind,
                    "reference": source_reference,
                },
                "license": {
                    "id": license_id,
                    "attribution": attribution,
                    "redistributable": redistributable,
                },
                "capture_params": {
                    "width": image.width,
                    "height": image.height,
                    "augmentation": AUGMENTATION_VERSION,
                    "pillow_version": PILLOW_VERSION,
                    **asdict(params),
                },
            }
        )
    return entries


def write_manifest(entries: list[dict], path: Path) -> None:
    """Overwrite ``path`` with stable, one-object-per-line JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)
    path.write_text(text, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate deterministic MJPEG/KVM-style dataset variants."
    )
    parser.add_argument("input", type=Path, help="source JPEG or PNG")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=ALL_PHASES, required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--split", choices=SPLITS, default="train")
    parser.add_argument(
        "--source-kind",
        choices=SOURCE_KINDS,
        default="synthetic",
    )
    parser.add_argument(
        "--source-reference",
        help="public source identifier; defaults to the input filename, never its absolute path",
    )
    parser.add_argument("--license", dest="license_id", required=True)
    parser.add_argument("--attribution", default="")
    parser.add_argument(
        "--redistributable",
        action="store_true",
        help="assert that the source license permits redistributing generated variants",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="JSONL destination; defaults to <output-dir>/manifest.jsonl",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    try:
        entries = build_variants(
            args.input,
            args.output_dir,
            phase=args.phase,
            count=args.count,
            seed=args.seed,
            split=args.split,
            source_kind=args.source_kind,
            source_reference=args.source_reference or args.input.name,
            license_id=args.license_id,
            attribution=args.attribution,
            redistributable=args.redistributable,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    manifest = args.manifest or args.output_dir / "manifest.jsonl"
    write_manifest(entries, manifest)
    print(f"wrote {len(entries)} variants and {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
