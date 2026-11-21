# MonkeyTester-Plugin-Esp32

The `esp32-sensor` device plugin for
[MonkeyTester](https://github.com/yurivibes/MonkeyTester). It owns
the **`role: sensor`** ESP32 boards plugged into its host, flashes prebuilt
firmware onto them, and relays a test's expectations (how the board should
answer the app: a broken sensor reading, an HTTP error, a hang, going offline,
…) to the board.

Hub/server boards and the fake software server are **not here**: they belong to
the sibling plugin [MonkeyTester-Plugin-Server](https://github.com/yurivibes/MonkeyTester-Plugin-Server)
(`esp32-server`, on :8093). The two plugins discover the same nodes but split
them by role: each probes a board's firmware `status`, keeps only its own role,
and shares the boards through per-node console locks and a role cache, so two
plugin processes never open the same serial console.

MonkeyTester reaches it only through the shared **Device Plugin API**
(`PLUGIN_API.md` in the MonkeyTester repo). Boards are driven over their serial
console with the **VT control protocol** (`docs/vt-control.md` in
the sensor firmware),
which that firmware's `…-test` builds include.

```
MonkeyTester ──HTTP (plugin API)──► esp32_plugin.py :8091 (Linux or macOS)
                                     ├── esptool ───────────► flash
                                     └── serial (VT control) ► ESP32 sensor boards
                                        (hub/server boards → esp32-server plugin :8093)
```

A board's **role** (`sensor` or `hub`, from its firmware's status; see
`docs/hub-protocol.md` in the server firmware) decides which plugin owns it.
Here, `device=sensor` (the default) or `device=<mac>`; `device=hub`/`server` is
answered with `wrong_plugin` — run the step against the esp32-server plugin
instead. When this plugin finds a hub board it drops it and releases its
console lock so esp32-server can adopt it.

## Install

| Dependency | Why | Required |
|---|---|---|
| Python 3.10 or newer | the plugin | yes |
| pyserial | the boards' serial consoles | yes |
| esptool | `flash`, and resetting native USB-Serial-JTAG boards | yes |
| Membership of the `dialout` group | opening `/dev/ttyUSB*` / `/dev/ttyACM*` | on Linux |

```bash
git clone https://github.com/yurivibes/MonkeyTester-Plugin-Esp32.git ~/Workspace/MonkeyTester-Esp32-Sensor
cd ~/Workspace/MonkeyTester-Esp32-Sensor
```

`~/Workspace/MonkeyTester-Esp32-Sensor` is the path the systemd unit in `packaging/` expects.

### With Nix (Linux or macOS)

Needs Nix with flakes enabled (`experimental-features = nix-command flakes` in
`~/.config/nix/nix.conf`). The flake brings Python, the Python packages and esptool, so
there is nothing else to install:

```bash
nix run .#tests            # unit tests against fake boards
nix run                    # the plugin on 127.0.0.1:8091 (loopback only)
nix develop                # a shell with python3 + the packages + esptool
```

### Without Nix

**Debian / Ubuntu:**

```bash
sudo apt-get install -y python3 python3-venv git
python3 -m venv .venv
.venv/bin/pip install pyserial esptool
```

**macOS (Homebrew):**

```bash
brew install python git
python3 -m venv .venv
.venv/bin/pip install pyserial esptool
```

The plugin finds esptool on `PATH`, else runs `<its python> -m esptool`, so the venv's copy
is enough.

### Serial-port access (Linux)

```bash
sudo usermod -aG dialout "$USER"   # then log out and back in
id -nG | grep -w dialout           # prints dialout when it worked
```

### Verify

```bash
# With Nix
nix run .#tests
nix develop -c esptool version

# Without Nix
.venv/bin/python -c "import serial, esptool"
.venv/bin/python -m unittest discover -s tests
```

The tests use a fake board and a fake esptool, so they need no hardware. Then start the
plugin (below) and check `curl -s localhost:8091/health`.

## Run it

Install the dependencies first (see [Install](#install)), then:

```bash
nix run                          # plugin on 127.0.0.1:8091 (loopback only)
.venv/bin/python esp32_plugin.py # the same, without Nix
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

### Network access and the token

The plugin API listens on **127.0.0.1 only** by default (`PLUGIN_BIND`), which is
all the viewer needs when it runs on the same host (it reaches the plugin at
`http://127.0.0.1:8091`). The sibling esp32-server plugin has the same defaults.

To reach the plugin from another machine, bind it to a LAN address **and** set a
token, or it refuses to start (exit code 2, with the reason on stderr):

```bash
export PLUGIN_TOKEN=$(openssl rand -hex 32)    # at least 16 characters
PLUGIN_BIND=0.0.0.0 PLUGIN_TOKEN=$PLUGIN_TOKEN nix run
curl -s -H "X-Plugin-Token: $PLUGIN_TOKEN" http://<plugin-host>:8091/devices
```

With a token set, every request except `GET /health` needs the
`X-Plugin-Token` header (401 otherwise); on loopback the token is optional and,
when set, enforced the same way. The viewer keeps each plugin's token in
**Settings → Device Plugins** (MonkeyTester PR in progress; until it lands, the
viewer's `PLUGIN_TOKEN` / `VT_PLUGIN_TOKEN_<NAME>` environment). There is no
TLS: use a VPN or Tailscale to reach the plugin across untrusted networks.

`flash` only reads firmware from `ESP32_FIRMWARE_ROOT` (default: the working
directory), any extra directory listed in `ESP32_FIRMWARE_ROOTS`
(`:`-separated), the upload directory and `/nix/store`; other paths, `..` and
symlinks out of those roots are refused. Details and limits: [SECURITY.md](SECURITY.md).

To keep it running: `packaging/systemd/monkeytester-esp32-sensor.service` (Linux,
user unit) or `packaging/launchd/com.blackjackfruit.monkeytester-esp32-sensor.plist`
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
cp packaging/systemd/monkeytester-esp32-sensor.service ~/.config/systemd/user/
#   (edit WorkingDirectory if the checkout is not ~/Workspace/MonkeyTester-Esp32-Sensor)
systemctl --user daemon-reload
systemctl --user enable --now monkeytester-esp32-sensor   # start now and at login

# Day to day
systemctl --user stop monkeytester-esp32-sensor      # frees the sensor ports
systemctl --user start monkeytester-esp32-sensor
systemctl --user restart monkeytester-esp32-sensor   # e.g. after pulling a new version
systemctl --user status monkeytester-esp32-sensor
journalctl --user -u monkeytester-esp32-sensor -f    # live log
```

If the user manager's `PATH` has no `nix`, spell it out in `ExecStart` (e.g.
`~/.nix-profile/bin/nix --extra-experimental-features "nix-command flakes" run .`).

**macOS (LaunchAgent):** replace `PYTHON` and `CHECKOUT_DIR` in the plist, then

```bash
cp packaging/launchd/com.blackjackfruit.monkeytester-esp32-sensor.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.blackjackfruit.monkeytester-esp32-sensor.plist    # start
launchctl unload -w ~/Library/LaunchAgents/com.blackjackfruit.monkeytester-esp32-sensor.plist  # stop
tail -f /tmp/monkeytester-esp32-sensor.log
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

### Pointing MonkeyTester at it

```bash
VT_PLUGINS=esp32-sensor=http://<plugin-host>:8091,esp32-server=http://<plugin-host>:8093
```

(`<plugin-host>` is `127.0.0.1` when the viewer runs on the same host, which is
the default setup; anything else needs `PLUGIN_BIND` + `PLUGIN_TOKEN` as above.)

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
  `result/` of `nix build .#firmware-c3-debug-test` in the sensor firmware. The
  directory is a path on the plugin's host under one of the allowed firmware
  roots (see [Network access and the token](#network-access-and-the-token));
  when MonkeyTester runs elsewhere it can `POST /uploads` a zip of it and pass
  `upload:<id>` instead (only `.bin` and `.json` files are unpacked).
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
| `security.py` | Bind/token policy and request-input validators (MAC, serial port, job/upload ids, contained paths); identical in both ESP32 plugin repos. See [SECURITY.md](SECURITY.md). |
| `esp32_plugin.py` | Board discovery, role ownership, actions, flashing, `main()` |
| `board.py` | One board on one serial port: its transport (`port_kind`: UART bridge or native USB-Serial-JTAG), reader thread, VT control requests, reset |
| `tests/` | Contract and action tests with a fake board (`fake_board.py`: pre-role and sensor firmware, and a fake USJ port whose node index moves after the fake esptool resets), a fake esptool, and `test_ownership.py` for the board/role split against esp32-server |
