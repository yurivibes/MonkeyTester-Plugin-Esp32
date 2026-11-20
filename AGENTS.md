# MonkeyTester-Esp32-Sensor — Agent Guide

## Purpose
The `esp32-sensor` device plugin for MonkeyTester: flashes prebuilt firmware
onto the **sensor** ESP32 boards and drives them over the VT control serial
protocol, behind the shared Device Plugin API. Hub/server boards and the fake
software server are the sibling plugin's (MonkeyTester-Esp32-Server, `esp32-server`,
:8093). See README.md.

## Setup (do this first)
Install the dependencies with the steps in README.md → **Install** before running or testing
anything; that section is the single source for them. In short:
- **Nix available** (`command -v nix`): nothing to install. `nix run .#tests` runs the tests,
  `nix run` starts the plugin on :8091, `nix develop` gives a shell with Python, pyserial
  and esptool.
- **No Nix:** Python 3.10+, then a `.venv` with `pip install pyserial esptool`
  (README.md → Install → Without Nix).
- On Linux, opening real boards needs the `dialout` group; the tests don't (they use
  `tests/fake_board.py` and `tests/fake_esptool.py`).
- Don't start the plugin on this host to try a change while the live
  `monkeytester-esp32-sensor` service runs: it holds the boards' serial ports. The tests need no hardware.
- A new dependency goes into README.md → Install and `flake.nix` in the same PR. If a step in
  README.md → Install doesn't work as written, fix it in the same PR.

## Critical Rules
- **The plugin API is a contract.** `plugin_server.py` implements
  `PLUGIN_API.md` from the MonkeyTester repo. Don't add or change endpoints,
  job states or fields here without changing that document first (and bumping
  `contract_version` for breaking changes).
- **The device protocol is owned by the firmware.** Board commands and the rule
  language follow `docs/vt-control.md` in Esp32-Sensor and
  `docs/hub-protocol.md` in Esp32-Server (roles, hub API, sections 6-7).
  For boards, `expect` relays rule lines without interpreting them; don't add
  parsing or validation of board rules here.
- **Sensor boards only.** A node whose firmware `status` answers `role: hub` is
  probed, dropped and its console lock released so esp32-server adopts it; the
  shared role cache keeps the next scan from touching it again. Hub actions
  (`forget`) and the fake server live in the other repo — don't move them back.
  The lock dir and role cache defaults (`/tmp/vt-esp32-locks`,
  `/tmp/vt-esp32-roles.json`) must match esp32-server's exactly: they are the
  cross-process protocol.
- **Two transports, one protocol.** A board is either on a CP2102N UART bridge
  (`uart-bridge`, `/dev/ttyUSB*`) or on its own USB-Serial-JTAG interface
  (`usb-serial-jtag`, `/dev/ttyACM*`). On the native one the plugin never sets a
  baud rate and never touches DTR/RTS (pyserial raises there), resets with esptool
  rather than through a pin, and re-finds the board by its MAC when the kernel
  hands the interface a new node index. `board.py:port_kind` is the one helper
  that decides; ACTIONS.md documents it.
- **Must run on Linux and macOS.** No Linux-only paths or tools in code paths
  (port globs are per-OS in `DEFAULT_GLOBS`). Python stdlib + pyserial + esptool
  only — no zeroconf here (mDNS belongs to the fake server in the other repo).
- **No test logic.** The plugin doesn't know about apps or vision models; it only
  makes boards do things and reports whether they did.
- Keep ACTIONS.md in step with `_build_actions()`.
- **Keep the user informed of user-relevant changes.** A change is user-relevant
  when it changes what a person running the plugin by hand does or sees: how to
  install, start, stop or restart it (systemd unit, LaunchAgent, `nix run`),
  ports (8091 here), environment variables, board discovery and serial-port
  handling (e.g. it holding the ports open), the curl/HTTP commands in README.md,
  test step syntax (`PLUGIN: esp32-sensor …`), or required packages/groups
  (`dialout`). For every such change:
  - update README.md (and ACTIONS.md for actions) **in the same PR**; the README
    commands must work as written;
  - add a **"For the user"** section to the PR description saying in plain
    words what changed for them and what, if anything, they must do (e.g.
    restart the service, update the unit file);
  - call it out in the report to the user, not only in the PR.
  If a change is not user-relevant, say "For the user: no change" in the PR.
- Work in a git worktree on a feature branch and open a PR; never commit to
  master.

## Tests
`nix run .#tests` (or `python3 -m unittest discover -s tests` with pyserial
installed). They use `tests/fake_board.py` and `tests/fake_esptool.py`; the fakes
cover both transports — `FakeSerial` carries a port kind (a CDC-ACM one raises on
DTR/RTS, like pyserial does) and `UsjNodes` moves the fake `/dev/ttyACM*` index
after every fake-esptool run, so the native port's reset, rebind and reopening of
the port are tested without hardware. Only real-hardware specifics (esptool
against a chip, an RTS reset on the bridge) need a board attached. The fakes never
open a real port: tests pin `ESP32_PORTS`/`_candidate_ports` to fake nodes.
`test_ownership.py` covers the split: a hub-role node gets probed, dropped, its
lock freed and its role cached (the flock primitives are simulated there — one
process stands in for two).
