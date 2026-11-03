# VisualTester-Esp32

The `esp32` device plugin for
[VisualTester](http://192.168.125.70:3000/blackjackfruit/VisualTester). It owns the
ESP32 boards plugged into its host, flashes prebuilt firmware onto them, and puts
them into the state an app test needs (normal readings, a broken sensor, an HTTP
error, offline, …), then reports whether the board responded correctly.

VisualTester reaches it only through the shared **Device Plugin API**
(`PLUGIN_API.md` in the VisualTester repo). Boards are driven over their serial
console with the **VT control protocol** (`docs/vt-control.md` in
[TemperatureSensor](http://192.168.125.70:3000/blackjackfruit/TemperatureSensor)),
which that firmware's `…-test` builds include.

```
VisualTester ──HTTP (plugin API)──► esp32_plugin.py :8091 (Linux or macOS)
                                     ├── esptool ───────────► flash
                                     └── serial (VT control) ► ESP32 board(s)
```

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

On Linux the user needs to be in the `dialout` group. Boards are found by
globbing the usual USB-UART ports (`/dev/ttyUSB*`, `/dev/ttyACM*` on Linux;
`/dev/cu.usbserial-*`, `/dev/cu.SLAB_USBtoUART*`, `/dev/cu.usbmodem*` on macOS)
every 5 s; set `ESP32_PORTS` to pin them. Every environment variable is listed at
the top of `esp32_plugin.py`.

To keep it running: `packaging/systemd/visualtester-esp32.service` (Linux, user
unit) or `packaging/launchd/com.blackjackfruit.visualtester-esp32.plist` (macOS
LaunchAgent).

### Pointing VisualTester at it

```bash
VT_PLUGINS=esp32=http://<plugin-host>:8091
```

and, in a test:

```
PLUGIN: esp32 flash firmware_dir=/path/to/TemperatureSensor/result
PLUGIN: esp32 scenario name=sensor-fail
VERIFY: The temperature card shows a sensor error
```

## What it does

See [ACTIONS.md](ACTIONS.md) for every action and its params, and
`scenarios.json` for the named board states. In short:

- **Devices** are identified by MAC (learned from the firmware's `status`), and
  can also be addressed by serial port. One job runs per board at a time.
- **flash** takes a prebuilt ESP-IDF build directory: anything with a
  `flasher_args.json` in it or in its `build/` subdirectory, such as the
  `result/` of `nix build .#firmware-c3-debug-test` in TemperatureSensor. The
  directory is a path on the plugin's host; when VisualTester runs elsewhere it
  can `POST /uploads` a zip of it and pass `upload:<id>` instead.
- **scenario** runs the scenario's VT control primitives, then reads the board's
  status back and fails the job if the board isn't in the expected state. New
  scenarios are added to `scenarios.json`, no code change needed, as long as the
  firmware has the primitives.
- **serial-expect**, **http**, **status** check what the board is actually doing.

Job outcomes follow the contract: `passed`; `failed` when the board answered
wrong (or not at all over VT control); `timeout`; `error` when the plugin itself
couldn't do it (bad firmware dir, esptool failure, port gone).

## Layout

| File | What |
|---|---|
| `plugin_server.py` | The generic plugin API server: jobs, long-poll waits, events, uploads. No ESP32 code. |
| `esp32_plugin.py` | Board discovery, actions, flashing, `main()` |
| `board.py` | One board on one serial port: reader thread, VT control requests, reset |
| `scenarios.json` | Named board states built from VT control primitives |
| `tests/` | Contract and action tests with a fake board (`fake_board.py`) and a fake esptool |
