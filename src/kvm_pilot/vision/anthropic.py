"""
Anthropic (Claude) vision backend.

No model version is hard-coded. Resolution order for the model id:
  1. Explicit ``model=`` argument.
  2. ``KVM_PILOT_VISION_MODEL`` environment variable.
  3. Newest vision-capable model discovered at runtime via the Models API
     (GET /v1/models): the most recently created entry whose
     ``capabilities.image_input`` is not explicitly unsupported.
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
# Headroom for the JSON envelope plus a bounded raw_text transcription; a
# text-dense boot console overflows a smaller budget and truncates the JSON.
_DEFAULT_MAX_TOKENS = 1024


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
        # The key is validated lazily at first network use (see _headers), not at
        # construction, so a backend can be built for a path that never calls the
        # model — e.g. ScreenAnalyzer resolving power_off from a cheap gate.
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._api_base = api_base.rstrip("/")
        self._model = model or os.environ.get("KVM_PILOT_VISION_MODEL") or None

    @property
    def model(self) -> str:
        if self._model is None:
            self._model = self._resolve_latest_model()
        return self._model

    @property
    def credentialed(self) -> bool:
        """Whether an API key is configured. False means every network call will
        raise VisionError; server-side wait loops check this up front (#147) to
        fail fast instead of retrying a credential error until their deadline.
        Duck-typed from the MCP server via ``getattr(backend, "credentialed", True)``,
        so a backend without the property is simply treated as credentialed."""
        return bool(self._api_key)

    # -- model discovery -------------------------------------------------

    def _resolve_latest_model(self) -> str:
        """Pick the newest vision-capable model from the Models API.

        The API lists entries newest-first. We return the first whose
        ``capabilities.image_input.supported`` is not explicitly ``false`` — a
        missing/absent capabilities tree is treated as usable, so older or
        proxied API responses keep the plain newest-first behavior. No fallback
        version string is hard-coded on purpose.
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

        for entry in models:
            mid = entry.get("id", "")
            if not mid:
                continue
            caps = entry.get("capabilities") or {}
            if (caps.get("image_input") or {}).get("supported") is False:
                continue  # explicitly no image input — can't classify screenshots
            return mid
        raise VisionError(
            "No vision-capable model found in the Anthropic Models API response. "
            "Pin one explicitly via model= or KVM_PILOT_VISION_MODEL."
        )

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
        # A max_tokens stop cuts the JSON mid-string; surface that specifically
        # instead of the misleading "did not return valid JSON" from the parser.
        if envelope.get("stop_reason") == "max_tokens":
            raise VisionError(
                f"Anthropic vision response truncated at max_tokens={self._max_tokens}; "
                "raise max_tokens= or shorten the on-screen text transcription"
            )
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
        if not self._api_key:
            raise VisionError(
                "No Anthropic API key. Pass api_key= or set ANTHROPIC_API_KEY."
            )
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
