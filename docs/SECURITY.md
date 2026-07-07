# Security Policy

`kvm-pilot` controls real hardware: it can power-cycle machines, mount boot
media, and inject keystrokes onto a console with no OS-level authentication on
the target. Treat it accordingly.

## Reporting a vulnerability

Please do not disclose vulnerability details in a public issue.

- **Preferred:** GitHub's private ["Report a vulnerability"](https://github.com/DustinTrap/kvm-pilot/security/advisories/new)
  flow under the repository's Security tab. Include reproduction steps and the
  affected version.
- **Fallback** (if that button is missing — private vulnerability reporting can
  be toggled off): open a regular issue saying only *"security report — please
  provide a private contact"*, **without any vulnerability details**, and a
  maintainer will arrange a private channel.

We aim to acknowledge reports within a few days. Do not include working
exploits against third-party devices or live hosts in a public report.

## Operational guidance

- **Do not expose a KVM device directly to the internet.** Put it behind a VPN
  or an authenticated reverse proxy. The PiKVM/GLKVM web stack is not hardened
  for direct public exposure.
- **Use TOTP/2FA** where the device supports it (`pip install
  "kvm-pilot[totp]"`). `verify_ssl` defaults to `False` because these devices
  ship self-signed certificates; set it to `True` and pin a real certificate if
  your environment allows.
- **Credentials** resolve from arguments, environment variables, or a config
  file. The library never writes secrets back out, and passwords plus session
  tokens are redacted from error text. Prefer environment injection over
  committing a config file with secrets. Avoid ``--passwd``/``--totp-secret`` on
  the command line — argv is visible to any local user via ``ps`` and persists in
  shell history; use ``KVM_PILOT_PASSWD`` / a profile, ``--passwd-file``, or
  ``--ask-passwd`` instead. If the config file holds a password or TOTP secret,
  restrict it (``chmod 600``); the CLI/library warn when it is group/other-readable.
- **The safety layer is advisory, not a sandbox.** `dry_run` and the
  confirmation callback prevent *accidental* destructive calls; they are not a
  privilege boundary. Anyone who can run your script with valid credentials can
  disable them.

## Untrusted screen content (prompt injection)

The vision layer transcribes whatever is on the target's screen — including a
`raw_text` field — and a compromised or hostile machine can display text crafted
to manipulate an LLM agent that consumes the classification (for example, text
telling the agent to power-cycle or type a command). Treat **all screen content
as untrusted input**, exactly like a web page an agent scrapes.

What the design guarantees today:

- **A classification never triggers a destructive action on its own.** `classify()`
  and `wait_for_state()` return *data*; they never call power, HID, virtual-media,
  or GPIO. Every state-changing op routes through `SafetyPolicy.guard`, and you
  wire those calls yourself (a regression test asserts a hostile classification
  result causes no device-state change).
- **`raw_text` is length-bounded** (the prompt caps the transcription) so a screen
  can't flood an agent's context.
- **Over MCP, every destructive tool is disabled until the operator opts its
  effect class in** via the server's own environment — `power` behind
  `KVM_PILOT_MCP_ALLOW_POWER`; the act tools `type_text`/`press_key`/
  `send_shortcut`/`mouse` behind `KVM_PILOT_MCP_ALLOW_HID`;
  `mount_iso`/`eject` behind `KVM_PILOT_MCP_ALLOW_MEDIA`; `ssh_exec` behind
  `KVM_PILOT_MCP_ALLOW_SSH` — and each invocation additionally requires
  approval (a human elicitation, or `confirm=true` under a standing policy).
  A screen-injected agent cannot set server env vars, and a reboot chord
  (Ctrl+Alt+Del) is classified by *effect*, so it needs the power gate even
  over the HID transport.

What you must still do: if you wire an agent to act on classifications, gate every
destructive step behind human/policy approval — do not let free text from the
screen select an action. The MCP server's per-invocation approval model
([#61](https://github.com/DustinTrap/kvm-pilot/issues/61)) is the structural
version of this.

## Scope

In scope: credential handling, secret redaction, the safety-gating logic, and
injection issues in how `kvm-pilot` constructs requests. Out of scope:
vulnerabilities in PiKVM, GLKVM, GL.iNet firmware, or third-party VLM servers —
report those to their respective projects.
