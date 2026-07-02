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

## Scope

In scope: credential handling, secret redaction, the safety-gating logic, and
injection issues in how `kvm-pilot` constructs requests. Out of scope:
vulnerabilities in PiKVM, GLKVM, GL.iNet firmware, or third-party VLM servers —
report those to their respective projects.
