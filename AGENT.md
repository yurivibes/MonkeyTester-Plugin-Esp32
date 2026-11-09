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
  language follow `docs/vt-control.md` in TemperatureSensor and
  `docs/hub-protocol.md` in TemperatureSensor-Hub (roles, hub API, sections 6-7).
  For boards, `expect` relays rule lines without interpreting them; don't add
  parsing or validation of board rules here.
- **The fake hub is the one exception:** `fake_hub.py` implements hub-protocol.md
  sections 1-4 and the hub rule language itself. Change it only together with
  that spec, and keep it behaving like the hub firmware (tests must not change
  between the fake and a real hub).
- **Two transports, one protocol.** A board is either on a CP2102N UART bridge
  (`uart-bridge`, `/dev/ttyUSB*`) or on its own USB-Serial-JTAG interface
  (`usb-serial-jtag`, `/dev/ttyACM*`). On the native one the plugin never sets a
  baud rate and never touches DTR/RTS (pyserial raises there), resets with esptool
  rather than through a pin, and re-finds the board by its MAC when the kernel
  hands the interface a new node index. `board.py:port_kind` is the one helper
  that decides; ACTIONS.md documents it.
- **Must run on Linux and macOS.** No Linux-only paths or tools in code paths
  (port globs are per-OS in `DEFAULT_GLOBS`). Python stdlib + pyserial + esptool,
  plus zeroconf for the fake hub's mDNS (imported lazily; tests mock it).
- **No test logic.** The plugin doesn't know about apps or vision models; it only
  makes boards do things and reports whether they did.
- Keep ACTIONS.md in step with `_build_actions()`.
- **Keep the user informed of user-relevant changes.** A change is user-relevant
  when it changes what a person running the plugin by hand does or sees: how to
  install, start, stop or restart it (systemd unit, LaunchAgent, `nix run`),
  ports (8091, fake hub 8092, mDNS), environment variables, board discovery and
  serial-port handling (e.g. it holding the ports open), the curl/HTTP commands
  in README.md, test step syntax (`PLUGIN: esp32 …`), or required
  packages/groups (`dialout`). For every such change:
  - update README.md (and ACTIONS.md for actions) **in the same PR**; the README
    commands must work as written;
  - add a **"For the user"** section to the PR description saying in plain
    words what changed for them and what, if anything, they must do (e.g.
    restart the service, update the unit file);
  - call it out in the report to the user, not only in the PR.
  If a change is not user-relevant, say "For the user: no change" in the PR.
- Work in a git worktree on a feature branch and open a PR; never commit to master.

## Tests
`nix run .#tests` (or `python3 -m unittest discover -s tests` with pyserial
installed). They use `tests/fake_board.py` and `tests/fake_esptool.py`; the fakes
cover both transports — `FakeSerial` carries a port kind (a CDC-ACM one raises on
DTR/RTS, like pyserial does) and `UsjNodes` moves the fake `/dev/ttyACM*` index
after every fake-esptool run, so the native port's reset, rebind and reopening of
the port are tested without hardware. Only real-hardware specifics (esptool
against a chip, an RTS reset on the bridge) need a board attached. The fakes never
open a real port: tests pin `ESP32_PORTS`/`_candidate_ports` to fake nodes.
The fake hub tests bind 127.0.0.1 on random ports and never touch real mDNS. To try
the fake hub by hand, run a scratch plugin on other ports with no boards
(`ESP32_PORTS=/dev/none PLUGIN_PORT=8191 ESP32_FAKE_HUB_PORT=8192`); it does
advertise `tshub-fake` on the LAN while it runs.
