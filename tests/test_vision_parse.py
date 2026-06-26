"""Tests for vision response parsing (no network)."""

import pytest

from kvm_pilot.errors import VisionError
from kvm_pilot.vision.base import (
    PHASE_GRUB_MENU,
    PHASE_UNKNOWN,
    parse_classification,
)


def test_parses_clean_json():
    text = '{"phase":"grub_menu","description":"GRUB","confidence":0.9,"raw_text":"GNU GRUB"}'
    state = parse_classification(text, "img")
    assert state.phase == PHASE_GRUB_MENU
    assert state.confidence == 0.9
    assert state.raw_text == "GNU GRUB"
    assert state.image_b64 == "img"


def test_strips_markdown_fences():
    text = '```json\n{"phase":"desktop","description":"d","confidence":0.8,"raw_text":""}\n```'
    state = parse_classification(text, "img")
    assert state.phase == "desktop"


def test_unknown_phase_normalised():
    text = '{"phase":"banana","description":"?","confidence":0.5,"raw_text":""}'
    state = parse_classification(text, "img")
    assert state.phase == PHASE_UNKNOWN


def test_bad_confidence_defaults_zero():
    text = '{"phase":"booting","description":"b","confidence":"high","raw_text":""}'
    state = parse_classification(text, "img")
    assert state.confidence == 0.0


def test_invalid_json_raises():
    with pytest.raises(VisionError):
        parse_classification("not json at all", "img")


def test_missing_fields_tolerated():
    state = parse_classification('{"phase":"power_off"}', "img")
    assert state.phase == "power_off"
    assert state.description == ""
    assert state.confidence == 0.0
