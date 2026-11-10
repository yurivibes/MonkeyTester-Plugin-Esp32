# VisualTester-Esp32-Sensor

The `esp32-sensor` device plugin for
[VisualTester](http://192.168.125.70:3000/blackjackfruit/VisualTester). It owns
the **`role: sensor`** ESP32 boards plugged into its host, flashes prebuilt
firmware onto them, and relays a test's expectations (how the board should
answer the app: a broken sensor reading, an HTTP error, a hang, going offline,
…) to the board.

Hub/server boards and the fake software server are **not here**: they belong to
the sibling plugin [VisualTester-Esp32-Server](http://192.168.125.70:3000/blackjackfruit/VisualTester-Esp32-Server)
(`esp32-server`, on :8093). The two plugins discover the same nodes but split
them by role: each probes a board's firmware `status`, keeps only its own role,
and shares the boards through per-node console locks and a role cache, so two
plugin processes never open the same serial console.

VisualTester reaches it only through the shared **Device Plugin API**
(`PLUGIN_API.md` in the VisualTester repo). Boards are driven over their serial
console with the **VT control protocol** (`docs/vt-control.md` in
[TemperatureSensor](http://192.168.125.70:3000/blackjackfruit/TemperatureSensor)),
which that firmware's `…-test` builds include.

```
VisualTester ──HTTP (plugin API)──► esp32_plugin.py :8091 (Linux or macOS)
                                     ├── esptool ───────────► flash
                                     └── serial (VT control) ► ESP32 sensor boards
                                        (hub/server boards → esp32-server plugin :8093)
```

A board's **role** (`sensor` or `hub`, from its firmware's status; see
`docs/hub-protocol.md` in TemperatureSensor-Hub) decides which plugin owns it.
Here, `device=sensor` (the default) or `device=<mac>`; `device=hub`/`server` is
answered with `wrong_plugin` — run the step against the esp32-server plugin
instead. When this plugin finds a hub board it drops it and releases its
console lock so esp32-server can adopt it.

## Run it

**Nix (Linux or macOS):**

```bash
nix run                    # plugin on 0.0.0.0:8091
nix run .#tests            # unit tests against a fake board
```

**Without Nix:** Python 3.10+, then

```bash
python3 -m venv .venv && .venv/bin/pip install pyserial esptool
.venv/bin/python esp32_plugin.py
```

Check it with `curl localhost:8091/health` and `curl localhost:8091/devices`.

On Linux the user needs to be in the `dialout` group. Boards are found by globbing
the usual USB ports (`/dev/serial/by-id/*`, `/dev/ttyUSB*`, `/dev/ttyACM*` on Linux;
`/dev/cu.usbserial-*`, `/dev/cu.SLAB_USBtoUART*`, `/dev/cu.usbmodem*` on macOS)
every 5 s; set `ESP32_PORTS` to pin them. A board is reached either through a
CP2102N UART bridge (`/dev/ttyUSB*`) or over its own USB-Serial-JTAG interface
(`/dev/ttyACM*`); `GET /devices` reports which in `details.port_kind`, together
with the node it is on. The native interface identifies the board before it
answers anything: its USB serial number is the board's MAC. Every environment
variable is listed at the top of `esp32_plugin.py`, including
`ESP32_LOCK_DIR` (default `/tmp/vt-esp32-locks`) and `ESP32_ROLE_CACHE`
(default `/tmp/vt-esp32-roles.json`) — the board-ownership files **shared**
with the esp32-server plugin: keep both plugins' settings of them in sync.

To keep it running: `packaging/systemd/visualtester-esp32-sensor.service` (Linux,
user unit) or `packaging/launchd/com.blackjackfruit.visualtester-esp32-sensor.plist`
(macOS LaunchAgent). Run the esp32-server plugin's unit alongside it; the split
happens by itself through the lock files and role cache.

### Start and stop

While it runs, the plugin keeps its boards' serial ports open. **Stop it before
you flash a board or open a serial monitor yourself** (`nix run .#monitor`,
esptool, screen, …), and start it again when you are done, or CI runs cannot
reach the boards.

**Linux (systemd user service):**

```bash
# Install once
cp packaging/systemd/visualtester-esp32-sensor.service ~/.config/systemd/user/
#   (edit WorkingDirectory if the checkout is not ~/Workspace/VisualTester-Esp32-Sensor)
systemctl --user daemon-reload
systemctl --user enable --now visualtester-esp32-sensor   # start now and at login

# Day to day
systemctl --user stop visualtester-esp32-sensor      # frees the sensor ports
systemctl --user start visualtester-esp32-sensor
systemctl --user restart visualtester-esp32-sensor   # e.g. after pulling a new version
systemctl --user status visualtester-esp32-sensor
journalctl --user -u visualtester-esp32-sensor -f    # live log
```

If the user manager's `PATH` has no `nix`, spell it out in `ExecStart` (e.g.
`~/.nix-profile/bin/nix --extra-experimental-features "nix-command flakes" run .`).

**macOS (LaunchAgent):** replace `PYTHON` and `CHECKOUT_DIR` in the plist, then

```bash
cp packaging/launchd/com.blackjackfruit.visualtester-esp32-sensor.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.blackjackfruit.visualtester-esp32-sensor.plist    # start
launchctl unload -w ~/Library/LaunchAgents/com.blackjackfruit.visualtester-esp32-sensor.plist  # stop
tail -f /tmp/visualtester-esp32-sensor.log
```

**In the foreground:** `nix run` (or `.venv/bin/python esp32_plugin.py`); stop it
with Ctrl-C. Only one copy can run at a time: stop the service first.

To see which port each board is on (useful before flashing by hand):

```bash
curl -s localhost:8091/devices   # details.role, details.port, details.mac
```

Without stopping the plugin you can still make the sensor advertise over BLE
through the plugin's own API (`seconds: 0` = until provisioned or a reboot):

```bash
curl -s -XPOST localhost:8091/jobs -H 'Content-Type: application/json' \
  -d '{"device":"sensor","action":"ble-advertise","params":{"seconds":0},"timeout_ms":60000}'
```

### Pointing VisualTester at it

```bash
VT_PLUGINS=esp32-sensor=http://<plugin-host>:8091,esp32-server=http://<plugin-host>:8093
```

and, in a test (sensor steps hit this plugin; hub/server steps hit the
other one):

```
PLUGIN: esp32-server factory-reset
PLUGIN: esp32-sensor factory-reset
PLUGIN: esp32-sensor ble-advertise seconds=0
# … the app provisions the sensor over BLE (BJF-<mac>); it reports to the server …
PLUGIN: esp32-server expect
  GET /devices -> sequence 17 18 19
END
VERIFY: The dashboard shows 17 °C
PLUGIN: esp32-sensor expect
  report off
END
VERIFY: The sensor shows as offline
PLUGIN: esp32-server clear
```

## What it does

See [ACTIONS.md](ACTIONS.md) for the actions (`flash`, `reset`,
`factory-reset`, `ble-advertise`, `broadcast-ssid`, `expect`, `clear`). In short:

- **Devices** are identified by MAC — learned from the firmware's `status`, or
  read off a native USB-Serial-JTAG port's USB serial number at discovery — and
  can also be addressed by serial port. `device=sensor` is the default and needs
  exactly one sensor board. One job runs per board at a time. A board whose node
  moved (esptool's hard reset on a native port) keeps its MAC and moves with it.
- **flash** takes a prebuilt ESP-IDF build directory: anything with a
  `flasher_args.json` in it or in its `build/` subdirectory, such as the
  `result/` of `nix build .#firmware-c3-debug-test` in TemperatureSensor. The
  directory is a path on the plugin's host; when VisualTester runs elsewhere it
  can `POST /uploads` a zip of it and pass `upload:<id>` instead.
- **expect** is a relay: the test's inline block goes to the board line by line
  as rules (`report`/`temp`/`wifi` directives on a sensor; HTTP response rules
  belong to the esp32-server plugin). A block states only what changes; earlier
  rules stay until overridden, undone with `-> normal`, or dropped with `clear`.
  The plugin doesn't know the rule language; the firmware does. A new failure
  case is a few lines in a test, with no plugin change.
- **clear** puts the board back to normal behaviour.
- **factory-reset** (a test's first device step) forgets the provisioned
  network and reboots; **ble-advertise** (firmware `prov-start`) starts BLE
  provisioning so the app can find the board as `BJF-<mac>` and provision it.
  Soft-AP provisioning builds use **broadcast-ssid** (`ap-start`) instead.

Job outcomes follow the contract: `passed`; `failed` when the board rejected a
rule or didn't answer over VT control; `timeout`; `error` when the plugin itself
couldn't do it (bad firmware dir, esptool failure, port gone).

## Layout

| File | What |
|---|---|
| `plugin_server.py` | The generic plugin API server: jobs, long-poll waits, events, uploads. No ESP32 code. |
| `esp32_plugin.py` | Board discovery, role ownership, actions, flashing, `main()` |
| `board.py` | One board on one serial port: its transport (`port_kind`: UART bridge or native USB-Serial-JTAG), reader thread, VT control requests, reset |
| `tests/` | Contract and action tests with a fake board (`fake_board.py`: pre-role and sensor firmware, and a fake USJ port whose node index moves after the fake esptool resets), a fake esptool, and `test_ownership.py` for the board/role split against esp32-server |
