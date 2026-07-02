"""Tests for the production vision backends (Anthropic + OpenAI-compatible).

Network is mocked at the backend's own HTTP seam, so these exercise payload
handling, truncation detection (#49), and model auto-resolution (#50) without a
key or a server.
"""

from __future__ import annotations

import pytest

from kvm_pilot.errors import VisionError
from kvm_pilot.vision.anthropic import AnthropicBackend
from kvm_pilot.vision.openai_compat import OpenAICompatBackend

_VALID = '{"phase":"grub_menu","description":"a menu","confidence":0.9,"raw_text":""}'


@pytest.fixture(autouse=True)
def _no_pinned_model(monkeypatch):
    # Auto-resolution tests must not pick up a pinned model from the environment.
    monkeypatch.delenv("KVM_PILOT_VISION_MODEL", raising=False)


# -- #49: truncation detection --------------------------------------------

def test_anthropic_max_tokens_stop_raises_truncation_error(monkeypatch):
    b = AnthropicBackend(api_key="k", model="m")
    monkeypatch.setattr(b, "_http_post_json", lambda path, payload: {
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": '{"phase":"booting","raw_text":"lots of te'}],
    })
    with pytest.raises(VisionError, match="truncated at max_tokens"):
        b.classify("img")


def test_anthropic_normal_stop_parses(monkeypatch):
    b = AnthropicBackend(api_key="k", model="m")
    monkeypatch.setattr(b, "_http_post_json", lambda path, payload: {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": _VALID}],
    })
    assert b.classify("img").phase == "grub_menu"


def test_openai_length_finish_raises_truncation_error(monkeypatch):
    import kvm_pilot.vision.openai_compat as mod
    monkeypatch.setattr(mod, "request_json", lambda *a, **k: {
        "choices": [{"finish_reason": "length", "message": {"content": '{"phase":"booti'}}],
    })
    with pytest.raises(VisionError, match="truncated at max_tokens"):
        OpenAICompatBackend("http://x/v1", "m").classify("img")


def test_openai_normal_finish_parses(monkeypatch):
    import kvm_pilot.vision.openai_compat as mod
    monkeypatch.setattr(mod, "request_json", lambda *a, **k: {
        "choices": [{"finish_reason": "stop", "message": {"content": _VALID}}],
    })
    assert OpenAICompatBackend("http://x/v1", "m").classify("img").phase == "grub_menu"


def test_openai_missing_finish_reason_still_parses(monkeypatch):
    # Some local servers omit finish_reason entirely.
    import kvm_pilot.vision.openai_compat as mod
    monkeypatch.setattr(mod, "request_json", lambda *a, **k: {
        "choices": [{"message": {"content": _VALID}}],
    })
    assert OpenAICompatBackend("http://x/v1", "m").classify("img").phase == "grub_menu"


def test_default_max_tokens_bumped_to_1024():
    assert AnthropicBackend(api_key="k")._max_tokens == 1024
    assert OpenAICompatBackend("http://x/v1", "m")._max_tokens == 1024


# -- #50: vision-capable model resolution ---------------------------------

def test_resolution_skips_non_vision_first_entry(monkeypatch):
    b = AnthropicBackend(api_key="k")
    monkeypatch.setattr(b, "_http_get_json", lambda path: {"data": [
        {"id": "text-only", "capabilities": {"image_input": {"supported": False}}},
        {"id": "vision-model", "capabilities": {"image_input": {"supported": True}}},
    ]})
    assert b.model == "vision-model"


def test_resolution_missing_capabilities_keeps_first(monkeypatch):
    # Older/proxied responses without a capabilities tree keep newest-first.
    b = AnthropicBackend(api_key="k")
    monkeypatch.setattr(b, "_http_get_json", lambda path: {"data": [
        {"id": "first"}, {"id": "second"},
    ]})
    assert b.model == "first"


def test_resolution_all_non_vision_raises_pin_hint(monkeypatch):
    b = AnthropicBackend(api_key="k")
    monkeypatch.setattr(b, "_http_get_json", lambda path: {"data": [
        {"id": "a", "capabilities": {"image_input": {"supported": False}}},
        {"id": "b", "capabilities": {"image_input": {"supported": False}}},
    ]})
    with pytest.raises(VisionError, match="[Pp]in"):
        _ = b.model


def test_resolution_empty_list_raises(monkeypatch):
    b = AnthropicBackend(api_key="k")
    monkeypatch.setattr(b, "_http_get_json", lambda path: {"data": []})
    with pytest.raises(VisionError, match="no models"):
        _ = b.model


# -- #51: retryable-status + Retry-After ----------------------------------

def test_request_json_429_carries_status_and_retry_after(monkeypatch):
    import email.message
    import io
    import urllib.error

    from kvm_pilot.vision.base import request_json

    hdrs = email.message.Message()
    hdrs["Retry-After"] = "7"

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "rate", hdrs, io.BytesIO(b'{"e":"x"}'))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(VisionError) as ei:
        request_json("POST", "http://x", headers={}, timeout=1, payload={})
    assert ei.value.status_code == 429
    assert ei.value.retry_after == 7.0


def test_request_json_429_http_date_retry_after_is_none(monkeypatch):
    import email.message
    import io
    import urllib.error

    from kvm_pilot.vision.base import request_json

    hdrs = email.message.Message()
    hdrs["Retry-After"] = "Wed, 21 Oct 2026 07:28:00 GMT"  # HTTP-date form

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "rate", hdrs, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(VisionError) as ei:
        request_json("POST", "http://x", headers={}, timeout=1, payload={})
    assert ei.value.status_code == 429
    assert ei.value.retry_after is None
