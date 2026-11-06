# VisualTester-Esp32

The `esp32` device plugin for
[VisualTester](http://192.168.125.70:3000/blackjackfruit/VisualTester). It owns the
ESP32 boards plugged into its host, flashes prebuilt firmware onto them, and
relays a test's expectations (how the board should answer the app: a broken
sensor reading, an HTTP error, a hang, going offline, …) to the board.

VisualTester reaches it only through the shared **Device Plugin API**
(`PLUGIN_API.md` in the VisualTester repo). Boards are driven over their serial
console with the **VT control protocol** (`docs/vt-control.md` in
[TemperatureSensor](http://192.168.125.70:3000/blackjackfruit/TemperatureSensor)),
which that firmware's `…-test` builds include.

```
VisualTester ──HTTP (plugin API)──► esp32_plugin.py :8091 (Linux or macOS)
                                     ├── esptool ───────────► flash
                                     ├── serial (VT control) ► ESP32 board(s): sensor, hub
                                     └── fake hub :8092 + mDNS tshub-fake (no hub board attached)
```

Boards have a **role** (`sensor` or `hub`, from their firmware's status; see
`docs/hub-protocol.md` in TemperatureSensor-Hub). Test steps pick one with
`device=sensor` (the default) or `device=hub`. When no hub board is attached,
`device=hub` is the plugin's **fake hub**: a software hub (HTTP API, registry,
history, rule language) on port 8092, advertised over mDNS as `tshub-fake`, so
the app and a real sensor on the LAN use it like a hub board.

## Run it

**Nix (Linux or macOS):**

```bash
nix run                    # plugin on 0.0.0.0:8091
nix run .#tests            # unit tests against a fake board
```

**Without Nix:** Python 3.10+, then

```bash
python3 -m venv .venv && .venv/bin/pip install pyserial esptool zeroconf
.venv/bin/python esp32_plugin.py
```

Check it with `curl localhost:8091/health` and `curl localhost:8091/devices`.

On Linux the user needs to be in the `dialout` group. Boards are found by globbing
the usual USB ports (`/dev/serial/by-id/*`, `/dev/ttyUSB*`, `/dev/ttyACM*` on Linux;
`/dev/cu.usbserial-*`, `/dev/cu.SLAB_USBtoUART*`, `/dev/cu.usbmodem*` on macOS)
every 5 s; set `ESP32_PORTS` to pin them. A board is reached either through a
CP2102N UART bridge (`/dev/ttyUSB*`) or over its own USB-Serial-JTAG interface
(`/dev/ttyACM*`); `GET /devices` reports which in `details.port_kind`, together with
the node it is on. The native interface identifies the board before it answers
anything: its USB serial number is the board's MAC. Every environment variable is
listed at the top of `esp32_plugin.py`.

The fake hub needs `zeroconf` (in the flake's Python; `pip install zeroconf`
otherwise) and its port reachable from the LAN (phones and sensors connect to
it): open TCP 8092 and mDNS (UDP 5353) in the host firewall. Its settings:

| Variable | Default | |
|---|---|---|
| `ESP32_FAKE_HUB` | `auto` | `auto`: run while no `role: hub` board is attached; `on`: always (and `device=hub` is always the fake); `off`: never |
| `ESP32_FAKE_HUB_PORT` | `8092` | HTTP port |
| `ESP32_FAKE_HUB_BIND` | `0.0.0.0` | listen address |
| `ESP32_FAKE_HUB_ADDR` | default-route IPv4 | the address advertised over mDNS and reported as the hub's `ip` |

Check it with `curl localhost:8092/info.json`; `curl localhost:8091/devices`
lists it as `tshub-fake` with its URL and mDNS state.

To keep it running: `packaging/systemd/visualtester-esp32.service` (Linux, user
unit) or `packaging/launchd/com.blackjackfruit.visualtester-esp32.plist` (macOS
LaunchAgent).

### Pointing VisualTester at it

```bash
VT_PLUGINS=esp32=http://<plugin-host>:8091
```

and, in a test:

```
PLUGIN: esp32 factory-reset device=hub
PLUGIN: esp32 factory-reset device=sensor
PLUGIN: esp32 ble-advertise device=sensor seconds=0
# … the app provisions the sensor over BLE (BJF-<mac>); it reports to the hub …
PLUGIN: esp32 expect device=hub
  GET /devices -> sequence 17 18 19
END
VERIFY: The dashboard shows 17 °C
PLUGIN: esp32 expect device=sensor
  report off
END
VERIFY: The sensor shows as offline
PLUGIN: esp32 clear device=hub
```

## What it does

See [ACTIONS.md](ACTIONS.md) for the actions (`flash`, `reset`,
`factory-reset`, `ble-advertise`, `broadcast-ssid`, `expect`, `clear`,
`forget`), roles and the fake hub. In short:

- **Devices** are identified by MAC — learned from the firmware's `status`, or read
  off a native USB-Serial-JTAG port's USB serial number at discovery — and
  can also be addressed by serial port, or by role: `device=sensor` (the
  default; exactly one sensor must be attached) and `device=hub` (the hub board,
  else the fake hub). One job runs per board at a time. A board whose node moved
  (esptool's hard reset on a native port) keeps its MAC and moves with it.
- **flash** takes a prebuilt ESP-IDF build directory: anything with a
  `flasher_args.json` in it or in its `build/` subdirectory, such as the
  `result/` of `nix build .#firmware-c3-debug-test` in TemperatureSensor. The
  directory is a path on the plugin's host; when VisualTester runs elsewhere it
  can `POST /uploads` a zip of it and pass `upload:<id>` instead.
- **expect** is a relay: the test's inline block goes to the board line by line
  as rules (HTTP response rules on the hub, `report`/`temp` directives on a
  sensor). On the fake hub the plugin applies the hub rule language itself. A block states only what changes; earlier rules stay until
  overridden, undone with `-> normal`, or dropped with `clear`. The plugin
  doesn't know the rule language; the firmware does. A new failure case is a few
  lines in a test, with no plugin change.
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
| `esp32_plugin.py` | Board discovery, roles, actions, flashing, `main()` |
| `fake_hub.py` | The fake hub: hub HTTP API, registry/history, hub rule language, mDNS |
| `board.py` | One board on one serial port: its transport (`port_kind`: UART bridge or native USB-Serial-JTAG), reader thread, VT control requests, reset |
| `tests/` | Contract and action tests with a fake board (`fake_board.py`: old sensor, sensor and hub firmware, and a fake USJ port whose node index moves after the fake esptool resets) and a fake esptool; `test_hub.py` covers roles and the fake hub |
