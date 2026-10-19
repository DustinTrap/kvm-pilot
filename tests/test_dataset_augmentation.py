"""Dataset manifest + KVM/MJPEG augmentation tests for issue #15."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from kvm_pilot.vision.base import ALL_PHASES

# tools/ is repo-only tooling (not in the wheel), and plain `pytest` does not put
# the repo root on sys.path -- load the script by path, as test_build_wiki_hcl does.
_SPEC = importlib.util.spec_from_file_location(
    "augment", Path(__file__).parents[1] / "tools" / "dataset" / "augment.py"
)
assert _SPEC and _SPEC.loader
_augment = importlib.util.module_from_spec(_SPEC)
sys.modules["augment"] = _augment  # dataclasses resolve their module via sys.modules
_SPEC.loader.exec_module(_augment)

AUGMENTATION_VERSION = _augment.AUGMENTATION_VERSION
augment_image = _augment.augment_image
build_variants = _augment.build_variants
main = _augment.main
sample_params = _augment.sample_params


def _source(path: Path, size: tuple[int, int] = (96, 54)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size)
    image.putdata(
        [
            ((x * 11) % 256, (y * 17) % 256, ((x + y) * 7) % 256)
            for y in range(size[1])
            for x in range(size[0])
        ]
    )
    image.save(path, format="PNG")
    return path


def test_same_seed_is_byte_identical_and_keeps_dimensions(tmp_path):
    source = _source(tmp_path / "source.png")
    with Image.open(source) as image:
        params = sample_params(41, *image.size)
        first = augment_image(image, params)
        second = augment_image(image, params)

    assert first == second
    assert first.startswith(b"\xff\xd8\xff")
    with Image.open(io.BytesIO(first)) as result:
        assert result.size == (96, 54)
        assert result.format == "JPEG"


def test_different_seeds_change_the_encoded_frame(tmp_path):
    source = _source(tmp_path / "source.png")
    with Image.open(source) as image:
        first = augment_image(image, sample_params(1, *image.size))
        second = augment_image(image, sample_params(2, *image.size))

    assert first != second


def test_build_variants_records_reproducible_manifest_data(tmp_path):
    source = _source(tmp_path / "source.png")
    output = tmp_path / "variants"
    entries = build_variants(
        source,
        output,
        phase="grub_menu",
        count=2,
        seed=17,
        split="train",
        source_kind="synthetic",
        source_reference="qemu/grub-fixture",
        license_id="Apache-2.0",
        attribution="",
        redistributable=True,
    )

    assert [row["capture_params"]["seed"] for row in entries] == [17, 18]
    assert all(row["capture_params"]["augmentation"] == AUGMENTATION_VERSION for row in entries)
    assert all(row["source"]["reference"] == "qemu/grub-fixture" for row in entries)
    assert all(row["license"]["redistributable"] is True for row in entries)
    assert {row["source_sha256"] for row in entries} == {
        hashlib.sha256(source.read_bytes()).hexdigest()
    }
    for row in entries:
        encoded = (output / row["image"]).read_bytes()
        assert hashlib.sha256(encoded).hexdigest() == row["sha256"]
        assert row["capture_params"]["pillow_version"]


def test_cli_writes_jsonl_without_leaking_absolute_input_path(tmp_path):
    source = _source(tmp_path / "private-parent" / "bios.png")
    output = tmp_path / "out"

    assert (
        main(
            [
                str(source),
                "--output-dir",
                str(output),
                "--phase",
                "bios_menu",
                "--count",
                "2",
                "--seed",
                "9",
                "--license",
                "Apache-2.0",
            ]
        )
        == 0
    )

    rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert {row["source"]["reference"] for row in rows} == {"bios.png"}
    assert str(tmp_path) not in (output / "manifest.jsonl").read_text()
    assert rows[0]["license"]["redistributable"] is False


def test_schema_phase_enum_tracks_runtime_taxonomy():
    schema_path = Path(__file__).parents[1] / "tools/dataset/vision-dataset.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["phase"]["enum"] == ALL_PHASES


def test_tiny_image_and_zero_width_are_handled(tmp_path):
    source = _source(tmp_path / "tiny.png", size=(1, 1))
    with Image.open(source) as image:
        assert augment_image(image, sample_params(3, 1, 1)).startswith(b"\xff\xd8\xff")
    with pytest.raises(ValueError, match="positive"):
        sample_params(3, 0, 1)


def test_invalid_source_is_actionable(tmp_path):
    source = tmp_path / "not-an-image.bin"
    source.write_text("hello")
    with pytest.raises(ValueError, match="not a decodable image"):
        build_variants(
            source,
            tmp_path / "out",
            phase="booting",
            count=1,
            seed=0,
            split="test",
            source_kind="other",
            source_reference="fixture",
            license_id="NOASSERTION",
            attribution="",
            redistributable=False,
        )
