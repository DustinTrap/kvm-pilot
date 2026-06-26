"""
OpenAI-compatible vision backend.

Works with any server exposing POST /v1/chat/completions with image support:
LM Studio, Ollama (OpenAI-compat mode), vLLM, llama.cpp server, LocalAI, etc.

This is the zero-cost / on-prem path: KVM screenshots never leave your network,
and there is no per-frame API charge. The model name is whatever you loaded on
the server — there is nothing to hard-code.

Example (LM Studio on the workstation):
    OpenAICompatBackend(
        base_url="http://127.0.0.1:1234/v1",
        model="qwen2.5-vl-7b",
        api_key="lm-studio",  # most local servers ignore the value
    )
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..errors import VisionError
from .base import (
    SYSTEM_PROMPT,
    ScreenState,
    VisionBackend,
    build_user_text,
    parse_classification,
)


class OpenAICompatBackend(VisionBackend):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        max_tokens: int = 512,
        timeout: float = 120.0,
        temperature: float = 0.0,
    ):
        if not model:
            raise VisionError(
                "OpenAICompatBackend requires an explicit model name "
                "(the model you loaded on the local server)."
            )
        self._base = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY") or "not-needed"
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._temperature = temperature

    @property
    def model(self) -> str:
        return self._model

    def classify(self, image_b64: str, hint: str = "") -> ScreenState:
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_user_text(hint)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                },
            ],
        }
        req = urllib.request.Request(
            self._base + "/chat/completions",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                envelope = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise VisionError(f"Local VLM HTTP {exc.code}: {body[:400]}") from exc
        except urllib.error.URLError as exc:
            raise VisionError(
                f"Could not reach local VLM at {self._base}: {exc.reason}"
            ) from exc

        try:
            text = envelope["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise VisionError(
                f"Unexpected chat-completions response shape: {str(envelope)[:300]}"
            ) from exc
        return parse_classification(text, image_b64)


__all__ = ["OpenAICompatBackend"]
