"""
Pluggable vision backends for screen-state classification.

A VisionBackend takes a base64 JPEG (a KVM snapshot) plus a hint and returns a
ScreenState. Concrete backends:
  * AnthropicBackend     -> Claude vision via the Anthropic Messages API
  * OpenAICompatBackend  -> any OpenAI-compatible /chat/completions endpoint
                            (LM Studio, Ollama, vLLM, llama.cpp server, etc.)

No model version is hard-coded anywhere. The Anthropic backend resolves the
newest vision-capable model at runtime (overridable via env/arg); the
OpenAI-compatible backend uses whatever model name you give it.
"""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from ..errors import VisionError

# --- recognised boot/run phases -------------------------------------------

PHASE_NO_SIGNAL = "no_signal"
PHASE_POWER_OFF = "power_off"
PHASE_POST_SCREEN = "post_screen"
PHASE_BIOS_MENU = "bios_menu"
PHASE_UEFI_SHELL = "uefi_shell"
PHASE_GRUB_MENU = "grub_menu"
PHASE_BOOTING = "booting"
PHASE_LOGIN_PROMPT = "login_prompt"
PHASE_DESKTOP = "desktop"
# The OS has handed off and is running, but the specific on-screen state
# (a login prompt vs. a desktop vs. a headless console) is not distinguishable
# from the signal at hand. Emitted by the vision backend and mapped to from a
# BMC's structured BootProgress=OSRunning.
PHASE_OS_RUNNING = "os_running"
PHASE_INSTALLER_WELCOME = "installer_welcome"
PHASE_INSTALLER_PARTITIONING = "installer_partitioning"
PHASE_INSTALLER_PROGRESS = "installer_progress"
PHASE_INSTALLER_COMPLETE = "installer_complete"
PHASE_CRASH_SCREEN = "crash_screen"
PHASE_UNKNOWN = "unknown"

ALL_PHASES: list[str] = [
    PHASE_NO_SIGNAL,
    PHASE_POWER_OFF,
    PHASE_POST_SCREEN,
    PHASE_BIOS_MENU,
    PHASE_UEFI_SHELL,
    PHASE_GRUB_MENU,
    PHASE_BOOTING,
    PHASE_LOGIN_PROMPT,
    PHASE_DESKTOP,
    PHASE_OS_RUNNING,
    PHASE_INSTALLER_WELCOME,
    PHASE_INSTALLER_PARTITIONING,
    PHASE_INSTALLER_PROGRESS,
    PHASE_INSTALLER_COMPLETE,
    PHASE_CRASH_SCREEN,
    PHASE_UNKNOWN,
]

# ``ALL_PHASES`` is the single source of truth for the token vocabulary; the
# prompt interpolates it so the list can never drift from what the parser
# validates against. (Braces in the example are doubled to survive the f-string.)
SYSTEM_PROMPT = f"""\
You are a remote KVM screen classifier. You receive a JPEG screenshot from a
KVM console and classify the current boot/run phase of the remote machine.

Respond ONLY with a single valid JSON object, no other text, with exactly:
  phase        (string)  one of the known phase tokens below
  description  (string)  one or two sentences describing what is visible
  confidence   (float)   0.0 to 1.0
  raw_text     (string)  legible on-screen text, verbatim, truncated to at
                          most ~500 characters, or ""

Known phase tokens:
  {", ".join(ALL_PHASES)}

Example:
{{"phase":"grub_menu","description":"A GRUB boot menu lists several kernels.","confidence":0.95,"raw_text":"GNU GRUB version 2.06"}}
"""


def build_user_text(hint: str) -> str:
    base = "Classify the attached KVM screenshot."
    if hint:
        base += f"\nHint: {hint}"
    base += "\nReturn only the JSON object described in the system prompt."
    return base


def request_json(
    method: str,
    url: str,
    *,
    headers: dict,
    timeout: float,
    payload: dict | None = None,
    label: str = "vision backend",
) -> dict:
    """Send a JSON request over stdlib urllib, mapping failures to VisionError.

    Shared by the vision backends so the ``urlopen`` + HTTP/URL error-mapping
    boilerplate lives in exactly one place. ``label`` names the backend in the
    error message (e.g. ``"Anthropic API"`` / ``"Local VLM"``).
    """
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise VisionError(f"{label} HTTP {exc.code}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise VisionError(f"{label} network error: {exc.reason}") from exc
    except (OSError, http.client.HTTPException) as exc:
        # Read timeouts/resets surface raw from urllib; the VisionError contract
        # (wait loops catch it and keep polling) must hold for them too.
        raise VisionError(f"{label} connection failed: {exc!r}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionError(f"{label} returned non-JSON response: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise VisionError(f"{label} returned non-object JSON: {raw[:200]}")
    return parsed


class ScreenState:
    """Result of a single classification."""

    __slots__ = ("phase", "description", "confidence", "raw_text", "image_b64", "timestamp")

    def __init__(
        self,
        phase: str,
        description: str,
        confidence: float,
        raw_text: str,
        image_b64: str = "",
        timestamp: float | None = None,
    ):
        self.phase = phase
        self.description = description
        self.confidence = confidence
        self.raw_text = raw_text
        self.image_b64 = image_b64
        self.timestamp = timestamp if timestamp is not None else time.time()

    def to_dict(self, include_image: bool = False) -> dict:
        d = {
            "phase": self.phase,
            "description": self.description,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "timestamp": self.timestamp,
        }
        if include_image:
            d["image_b64"] = self.image_b64
        return d

    def __repr__(self) -> str:
        return f"ScreenState(phase={self.phase!r}, confidence={self.confidence:.2f})"


def _clamp_confidence(value: object) -> float:
    """Normalize a model-reported confidence to a finite 0.0–1.0 float.

    Local VLMs sometimes answer on a percent scale ("confidence": 95) — left
    unclamped that trivially defeats every min_confidence gate.
    """
    try:
        c = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(c):
        return 0.0
    if 1.0 < c <= 100.0:
        c /= 100.0
    return min(max(c, 0.0), 1.0)


def parse_classification(text: str, image_b64: str) -> ScreenState:
    """Parse a model's JSON text (tolerating ``` fences) into a ScreenState.

    This is backend-independent and is the unit under test for the vision path,
    so it never makes a network call.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.splitlines() if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise VisionError(f"Classifier did not return valid JSON: {cleaned[:200]}") from exc
    if not isinstance(data, dict):
        raise VisionError(f"Classifier returned non-object JSON: {cleaned[:200]}")

    phase = str(data.get("phase", PHASE_UNKNOWN)).lower()
    if phase not in ALL_PHASES:
        phase = PHASE_UNKNOWN

    confidence = _clamp_confidence(data.get("confidence"))

    return ScreenState(
        phase=phase,
        description=str(data.get("description", "")),
        confidence=confidence,
        raw_text=str(data.get("raw_text", "")),
        image_b64=image_b64,
    )


class VisionBackend(ABC):
    """Abstract base for a vision classification backend."""

    @abstractmethod
    def classify(self, image_b64: str, hint: str = "") -> ScreenState:
        """Classify a base64 JPEG and return a ScreenState."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The resolved model identifier this backend will use."""


__all__ = [
    "VisionBackend",
    "ScreenState",
    "parse_classification",
    "build_user_text",
    "request_json",
    "SYSTEM_PROMPT",
    "ALL_PHASES",
    "PHASE_NO_SIGNAL",
    "PHASE_POWER_OFF",
    "PHASE_POST_SCREEN",
    "PHASE_BIOS_MENU",
    "PHASE_UEFI_SHELL",
    "PHASE_GRUB_MENU",
    "PHASE_BOOTING",
    "PHASE_LOGIN_PROMPT",
    "PHASE_DESKTOP",
    "PHASE_OS_RUNNING",
    "PHASE_INSTALLER_WELCOME",
    "PHASE_INSTALLER_PARTITIONING",
    "PHASE_INSTALLER_PROGRESS",
    "PHASE_INSTALLER_COMPLETE",
    "PHASE_CRASH_SCREEN",
    "PHASE_UNKNOWN",
]
