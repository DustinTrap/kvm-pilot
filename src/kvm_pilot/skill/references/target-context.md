# Target context — whose locale, keyboard, and timezone? (#79)

> Part of the bundled kvm-pilot skill. Read this whenever an installer,
> first-boot setup, or answer file asks for language/locale, keyboard layout,
> or timezone. Also served at runtime by the MCP `doctrine` tool (topic
> "target-context").

The machine behind the KVM is not the machine the operator is sitting at. When
a flow asks for **language/locale, keyboard layout, or timezone** — an OS
installer's first screens, first-boot setup, or any answer file you generate —
**ask the user whether their local context applies to the target before
answering.** The target may be in another region (colo, remote DC, another
country) or destined for a different keyboard layout than the operator's laptop.

- Offer the operator's detected values as the **default-but-confirmable**
  answer, never a silent assumption. Detect them from `$LANG`,
  `localectl` / `timedatectl` (Linux), or `defaults read -g AppleLocale` +
  `readlink /etc/localtime` (macOS).
- One question covers the flow: *"Use this machine's settings (`en_US.UTF-8`,
  `us`, `America/Los_Angeles`) for the target, or configure it differently?"*
  Reuse the answer for every later locale/keyboard/timezone prompt in the same
  install rather than re-asking.
- **Keyboard layout also affects your own typing.** kvm-pilot sends text as HID
  scancodes translated with a US keymap (library default `keymap="en-us"`;
  the MCP and CLI act tools don't expose a keymap option), and the target
  decodes scancodes per *its* configured layout. If the user picks a non-US
  layout for the target, later `type_text` symbols/passwords can land wrong —
  prefer `press_key` navigation, or hand off to SSH once it's up.
