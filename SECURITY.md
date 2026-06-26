# Security Policy

`kvm-pilot` controls real hardware: it can power-cycle machines, mount boot
media, and inject keystrokes onto a console with no OS-level authentication on
the target. Treat it accordingly.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub's **"Report a
vulnerability"** flow under the Security tab, rather than opening a public
issue. Include reproduction steps and the affected version. We aim to
acknowledge reports within a few days.

Do not include working exploits against third-party devices or live hosts in a
public report.

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
  tokens are redacted from error text. Still, prefer environment injection over
  committing a config file with secrets.
- **The safety layer is advisory, not a sandbox.** `dry_run` and the
  confirmation callback prevent *accidental* destructive calls; they are not a
  privilege boundary. Anyone who can run your script with valid credentials can
  disable them.

## Scope

In scope: credential handling, secret redaction, the safety-gating logic, and
injection issues in how `kvm-pilot` constructs requests. Out of scope:
vulnerabilities in PiKVM, GLKVM, GL.iNet firmware, or third-party VLM servers —
report those to their respective projects.
