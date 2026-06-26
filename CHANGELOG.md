# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — unreleased

Initial public release.

### Added
- `KVMClient`: full PiKVM / GLKVM REST client covering auth (incl. TOTP/2FA),
  keyboard + mouse HID, snapshots/OCR, ATX power, Mass Storage Device, GPIO,
  Redfish, WebSocket events, and system info/logs/metrics.
- Safety layer (`SafetyPolicy`): `dry_run` mode and a confirmation callback
  gating an explicit, auditable set of destructive operations.
- Pluggable vision subsystem with two backends:
  - `AnthropicBackend` — resolves the newest vision-capable model at runtime
    via the Models API; no hard-coded version. Override with
    `KVM_PILOT_VISION_MODEL` or `model=`.
  - `OpenAICompatBackend` — any OpenAI-compatible endpoint (LM Studio, Ollama,
    vLLM, llama.cpp) for zero-cost on-prem inference.
- `ScreenAnalyzer`: backend-agnostic single-shot classification plus blocking
  `wait_for_state` / `wait_for_any_state` loops with confidence thresholds and
  bounded backoff.
- `kvm-pilot` CLI with `info`, `snapshot`, `power`, `power-cycle`, `type`,
  `key`, `mount`, `classify`, and `watch` subcommands; interactive confirmation
  by default, `--yes` and `--dry-run` flags.
- Config resolution (`resolve_host`) with args > env > TOML-profile precedence.
- HTTP transport with bounded retry/backoff on transient errors (409/503/
  network) and password/token redaction in error text.

### Notes
- Tested against GL-RM1PE. PiKVM v3/v4, BliKVM, and GL-RM1 are expected to work
  but are not yet verified.

[0.1.0]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0
