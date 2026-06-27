"""Pluggable vision subsystem for kvm-pilot."""

from __future__ import annotations

from ..errors import VisionError
from .analyzer import DEFAULT_OCR_RULES, ScreenAnalyzer
from .anthropic import AnthropicBackend
from .base import (
    ALL_PHASES,
    SYSTEM_PROMPT,
    ScreenState,
    VisionBackend,
    parse_classification,
)
from .openai_compat import OpenAICompatBackend


def make_backend(
    kind: str = "anthropic",
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    **kw,
) -> VisionBackend:
    """Factory: build a backend by name.

    kind="anthropic"  -> AnthropicBackend (model auto-resolved if not given)
    kind="local"/"openai" -> OpenAICompatBackend (base_url + model required)
    """
    kind = kind.lower()
    if kind in ("anthropic", "claude"):
        return AnthropicBackend(api_key=api_key, model=model, **kw)
    if kind in ("local", "openai", "openai_compat", "lmstudio", "ollama", "vllm"):
        # VisionError (a KVMPilotError) so a CLI misconfiguration surfaces as a
        # clean message instead of an uncaught ValueError traceback.
        if not base_url:
            raise VisionError(f"backend kind={kind!r} requires base_url=")
        if not model:
            raise VisionError(f"backend kind={kind!r} requires model=")
        return OpenAICompatBackend(base_url=base_url, model=model, api_key=api_key, **kw)
    raise ValueError(f"Unknown vision backend kind: {kind!r}")


__all__ = [
    "ScreenAnalyzer",
    "DEFAULT_OCR_RULES",
    "VisionBackend",
    "AnthropicBackend",
    "OpenAICompatBackend",
    "ScreenState",
    "make_backend",
    "parse_classification",
    "ALL_PHASES",
    "SYSTEM_PROMPT",
]
