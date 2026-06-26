---
name: Bug report
about: Something isn't working
labels: bug
---

**What happened**
A clear description of the bug and what you expected instead.

**Device / firmware**
- Device: (e.g. GL-RM1PE, PiKVM v4, BliKVM)
- Firmware version:
- Did you enable the PiKVM REST API? (GL firmware disables it by default — see README)

**Environment**
- kvm-pilot version: (`kvm-pilot --version`)
- Python version:
- Backend: anthropic / local (which server + model)

**Reproduction**
Steps or a minimal snippet. Redact credentials.

**Logs**
Run with `-v` / set logging to DEBUG and paste relevant output (credentials are
redacted automatically, but double-check before posting).
