# Boot-phase dataset tooling

This directory contains hardware-independent helpers for the labeled vision
dataset proposed in issue #15. The first tool turns a clean synthetic frame into
deterministic variants that resemble common KVM capture defects:

- downscale/upscale softness;
- JPEG quality and chroma-subsampling loss;
- brightness and gamma shifts;
- black capture borders;
- alternating scanlines; and
- a horizontally displaced band that approximates a torn partial frame.

It does **not** anonymize a screenshot. Do not feed it real login prompts,
desktops, serial numbers, IP addresses, or other sensitive frames until a
separate PII-review step has approved them.

## Generate variants

Install the project first, then run the tool from the repository root:

```bash
pip install -e .
python tools/dataset/augment.py clean-grub.png \
  --output-dir /tmp/kvm-variants \
  --phase grub_menu \
  --count 8 \
  --seed 2026 \
  --source-kind synthetic \
  --source-reference qemu/ovmf/grub-2.12 \
  --license Apache-2.0 \
  --redistributable
```

The command writes JPEG variants plus `manifest.jsonl`. Each line follows
[`vision-dataset.schema.json`](vision-dataset.schema.json) and records the
class label, source, license, source/output SHA-256 digests, Pillow version, and
every sampled transform. Running the same source image with the same seed and
Pillow version produces byte-identical JPEGs and manifest entries.

`--redistributable` is deliberately opt-in. Leaving it off records `false`; it
does not prevent private evaluation but makes the licensing status explicit.
