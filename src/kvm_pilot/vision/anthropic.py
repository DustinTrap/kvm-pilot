"""
Anthropic (Claude) vision backend.

No model version is hard-coded. Resolution order for the model id:
  1. Explicit ``model=`` argument.
  2. ``KVM_PILOT_VISION_MODEL`` environment variable.
  3. Newest vision-capable model discovered at runtime via the Models API
     (GET /v1/models), picking the most recently created entry.
  4. As a last resort, if the Models API is unreachable, raise a clear error
     telling the user to pin a model — we deliberately do NOT bake in a guess
     that could rot.

The Messages API call uses only the Python standard library.
"""

from __future__ import annotations

import os

from ..errors import VisionError
from .base import (
    SYSTEM_PROMPT,
    ScreenState,
    VisionBackend,
    build_user_text,
    parse_classification,
    request_json,
)

_API_BASE = "https://api.anthropic.com"
_MESSAGES = "/v1/messages"
_MODELS = "/v1/models"
_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 512


class AnthropicBackend(VisionBackend):
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: float = 60.0,
        api_base: str = _API_BASE,
    ):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self._api_key:
            raise VisionError(
                "No Anthropic API key. Pass api_key= or set ANTHROPIC_API_KEY."
            )
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._api_base = api_base.rstrip("/")
        self._model = model or os.environ.get("KVM_PILOT_VISION_MODEL") or None

    @property
    def model(self) -> str:
        if self._model is None:
            self._model = self._resolve_latest_model()
        return self._model

    # -- model discovery -------------------------------------------------

    def _resolve_latest_model(self) -> str:
        """Pick the newest model from the Models API.

        The Models API returns entries newest-first. We take the first id that
        looks like a current general model (skips any we can't use). We do not
        hard-code a fallback version string on purpose.
        """
        try:
            data = self._http_get_json(_MODELS)
        except Exception as exc:  # noqa: BLE001
            raise VisionError(
                "Could not auto-resolve a vision model from the Anthropic Models "
                "API. Pin one explicitly via model= or KVM_PILOT_VISION_MODEL. "
                f"Underlying error: {exc}"
            ) from exc

        models: list[dict] = data.get("data", []) if isinstance(data, dict) else []
        if not models:
            raise VisionError("Anthropic Models API returned no models.")

        # The API lists newest first. Prefer a non-legacy entry; all current
        # Claude models accept image input, so the newest is the right default
        # for cost/latency the user can override upward if they want.
        for entry in models:
            mid = entry.get("id", "")
            if mid:
                return mid
        raise VisionError("No usable model id found in Models API response.")

    # -- classification --------------------------------------------------

    def classify(self, image_b64: str, hint: str = "") -> ScreenState:
        payload = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": build_user_text(hint)},
                    ],
                }
            ],
        }
        envelope = self._http_post_json(_MESSAGES, payload)
        text = ""
        for block in envelope.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                break
        if not text:
            raise VisionError(f"Anthropic response had no text block: {str(envelope)[:300]}")
        return parse_classification(text, image_b64)

    # -- stdlib HTTP -----------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": _VERSION,
        }

    def _http_post_json(self, path: str, payload: dict) -> dict:
        return request_json(
            "POST", self._api_base + path, headers=self._headers(),
            timeout=self._timeout, payload=payload, label="Anthropic API",
        )

    def _http_get_json(self, path: str) -> dict:
        return request_json(
            "GET", self._api_base + path, headers=self._headers(),
            timeout=self._timeout, label="Anthropic API",
        )


__all__ = ["AnthropicBackend"]
