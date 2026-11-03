# VisualTester-Esp32 — Agent Guide

## Purpose
The `esp32` device plugin for VisualTester: flashes prebuilt firmware onto ESP32
boards and drives them over the VT control serial protocol, behind the shared
Device Plugin API. See README.md.

## Critical Rules
- **The plugin API is a contract.** `plugin_server.py` implements
  `PLUGIN_API.md` from the VisualTester repo. Don't add or change endpoints,
  job states or fields here without changing that document first (and bumping
  `contract_version` for breaking changes).
- **The device protocol is owned by the firmware.** Board commands and the rule
  language follow `docs/vt-control.md` in TemperatureSensor. `expect` relays rule
  lines without interpreting them; don't add parsing or validation of rules here.
- **Must run on Linux and macOS.** No Linux-only paths or tools in code paths
  (port globs are per-OS in `DEFAULT_GLOBS`). Python stdlib + pyserial + esptool only.
- **No test logic.** The plugin doesn't know about apps or vision models; it only
  makes boards do things and reports whether they did.
- Keep ACTIONS.md in step with `_build_actions()`.
- Work in a git worktree on a feature branch and open a PR; never commit to master.

## Tests
`nix run .#tests` (or `python3 -m unittest discover -s tests` with pyserial
installed). They use `tests/fake_board.py` and `tests/fake_esptool.py`; real
hardware paths (esptool against a chip, RTS reset) need a board attached.
