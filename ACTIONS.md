# esp32-sensor plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in VisualTester). `GET /actions` returns the same list with full
parameter schemas.

## Devices and roles

This plugin owns the **`role: sensor`** boards only (TemperatureSensor-Hub
`docs/hub-protocol.md` sections 6 and 7 name the roles; the project calls the
`role: hub` boards "server"). Each board's `status` / `@vt - ready` line says
`"role":"sensor"` or `"role":"hub"`; firmware from before roles (no `role`
field), and boards that don't answer VT control, count as sensors. A node that
answers with the other role is probed, dropped and its console lock released so
the esp32-server plugin adopts it; the shared role cache keeps the next scan
from touching it.

`device=` in a test step (the job's `device`) is one of:

| Value | Resolves to |
|---|---|
| omitted, or `sensor` | The one attached sensor board. Fails (`unknown_device`, 404) when none is attached, and (`bad_request`, 400) when there are several: then pass the MAC |
| `hub` or `server` | `wrong_plugin` (404): hub/server boards and the fake server belong to the esp32-server plugin |
| a MAC, or a serial port | That board, when this plugin owns it |

`GET /devices` lists every owned board with `details.role`, `details.port` (the
serial node it is on) and `details.port_kind` (how it is reached: `uart-bridge`
through a CP2102N UART adapter, or `usb-serial-jtag` over the chip's own
USB-Serial-JTAG interface). Sensor entries carry the firmware's `hub`, `report`
and `last_report` status fields.

## The two transports

A board is reached over one of two serial transports (`board.py:port_kind`,
reported as `details.port_kind`):

| Port kind | Node | What it gives the plugin |
|---|---|---|
| `uart-bridge` | `/dev/ttyUSB*` (macOS `cu.usbserial*`): the CP2102N adapter | a baud rate (`ESP32_BAUD`, `ESP32_FLASH_BAUD`) and an EN/RTS pin to pulse |
| `usb-serial-jtag` | `/dev/ttyACM*` (macOS `cu.usbmodem*`): the chip's own interface | nothing to set (CDC-ACM ignores the baud rate and has no DTR/RTS, so the plugin touches neither), no pin to reset through, and a USB serial number that **is the board's MAC** (`AC:27:6E:FA:BB:5C` in udev's by-id name) |

A native board is therefore identified at discovery, before it answers VT
control. It has no reset button on the wire: `reset` runs esptool on that port,
which ends with its own hard reset. esptool may make the kernel re-create the
interface with another index (`/dev/ttyACM0` becomes `/dev/ttyACM1`), so the
plugin re-enumerates the nodes after every esptool run and the board follows its
MAC to the new node — `details.port` changes, its id and events don't. The
`baud` parameter and `ESP32_BAUD`/`ESP32_FLASH_BAUD` mean nothing there.

## The fake server — not here

The software server (hub HTTP API on :8092, registry/history, rule language,
mDNS `tshub-fake`) lives in the sibling **VisualTester-Esp32-Server** plugin
with its full documentation (`esp32-server` on :8093). Tests don't change
between the fake and a real hub board; they run against the esp32-server
plugin either way.

## Actions

| Action | Params | Passes when | Default timeout |
|---|---|---|---|
| `flash` | `firmware_dir` (path, required), `erase` (bool, false), `wait_ready` (bool, true), `baud` (int; ignored on a native port) | esptool succeeds and, with `wait_ready`, the board prints `@vt - ready` (or answers `status`) after reboot | 180 s |
| `reset` | `wait_ready` (bool, true) | The board reboots — EN is pulsed through the adapter's RTS pin on a `uart-bridge` port, esptool's own hard reset on a native `usb-serial-jtag` one — and prints `@vt - ready`. A reboot drops every rule | 30 s |
| `factory-reset` | — | The board forgets its provisioned Wi-Fi network, reboots, and comes back ready with no rules | 30 s |
| `broadcast-ssid` | `seconds` (int; 0 = until provisioned or reboot; default: the firmware's `PROVISION_TIMEOUT_S`) | The board reports its provisioning SSID (`BJF-<mac>`) is broadcasting; `result.ssid` is the name | 10 s |
| `ble-advertise` | `seconds` (int; 0 = until provisioned or reboot; default: the firmware's `PROVISION_TIMEOUT_S`) | The board answers `prov-start` and reports BLE provisioning is on; `result.name` is its BLE name (`BJF-<mac>`), `result.transport` is `ble`. Needs firmware with `prov-start` | 10 s |
| `expect` | `script` (string, required; the test's inline block) | The board accepted every rule line | 30 s |
| `clear` | — | The board dropped every rule and directive | 10 s |

Everything here runs on the sensor board; hub-board and fake-server behaviour
(`forget`, HTTP rules, hub `factory-reset`) belongs to the esp32-server plugin's
ACTIONS.md.

## Starting a test

Start every app test from factory settings, then let the app find the board.
With a server (hub-protocol.md section 7), reset both — hub steps hit the
esp32-server plugin, sensor steps hit this one — provision the sensor, then
state how the server answers the app:

```
PLUGIN: esp32-server factory-reset
PLUGIN: esp32-sensor factory-reset
PLUGIN: esp32-sensor ble-advertise seconds=0
# …the app finds BLE device BJF-<mac> and provisions the sensor; the sensor
# reports to the server (the fake one when no hub board is attached)…
PLUGIN: esp32-server expect
  GET /devices -> sequence 17 18 19
END
```

Before servers (one sensor board serving HTTP itself) it was just:

```
PLUGIN: esp32-sensor factory-reset
PLUGIN: esp32-sensor ble-advertise seconds=0
```

`factory-reset` erases the provisioned network (the board falls back to the
build's placeholder default, so it isn't on any real network) and drops every
rule. `ble-advertise` sends the firmware's `prov-start`, which turns provisioning
on as if the board's button had been held. In `debug` builds provisioning is
always on and the action only confirms it; in `release` builds it's what makes
the board visible.

Boards running a soft-AP provisioning build (the app joins the open Wi-Fi
network `BJF-<mac>` and POSTs `/setup`) use `broadcast-ssid` instead, which
sends `ap-start`. `ble-advertise` fails with `wrong_transport` on such a board,
and with `unsupported` on firmware older than `prov-start`.

## `expect`

The test's inline block states **only what changes** in how the board answers
the app. Everything not mentioned stays as it was:

```
PLUGIN: esp32-sensor expect
  # the sensor breaks, and provisioning starts failing
  GET /temperature.json -> 200 {"device":"{device}","temperature_c":0,"ok":false,"err":1,"ip":"{ip}"}
  POST /setup -> 500
END
VERIFY: The temperature card shows a sensor error
PLUGIN: esp32-sensor expect
  # the sensor recovers; /setup keeps failing
  GET /temperature.json -> normal
END
```

For a board, the plugin doesn't interpret the rules. It drops blank and `#`
lines and sends each line as `rule <line>` over the VT control protocol, nothing
else. The rule language belongs to the firmware: `docs/vt-control.md` in TemperatureSensor
(HTTP responses with a status and body, `hang`, `delay`, `sequence` for a
reading whose temperature moves on with each request, `normal` to undo one
rule, and `wifi`/`mqtt` directives). A later rule for the same method and path
replaces the earlier one.

**Which board gets which rules.** Since hubs, sensors have no HTTP server: a
sensor rejects HTTP rules (`no HTTP server; set rules on the hub`) and takes
the directives `report on|off` (stop/start `POST /report`, so the server sees it
go offline), `temp <t1> [<t2> …]` (report these values in turn instead of the
probe; 1-8 values, -55..125), `temp normal`, plus `wifi`/`mqtt on|off`. HTTP
rules go to the server — the esp32-server plugin, for `/devices`, `/devices/<mac>`,
`/devices/<mac>/history`, `/info.json` and `POST /report` (e.g.
`POST /report -> 500` makes the server refuse sensors):

```
PLUGIN: esp32-server expect
  GET /devices -> 200 []
END
VERIFY: The dashboard shows no sensors
PLUGIN: esp32-sensor expect
  report off
END
```

The fake server's rule language (the same as the hub firmware's: `<METHOD> <path> ->
<status> [<body>]`, `hang`, `delay <ms> <status> [<body>]`, `sequence <t1> …`,
`normal`) and its limits live in the esp32-server plugin's ACTIONS.md. A sensor
rejects server directives (`wifi`, `mqtt` are its own `on|off`; HTTP rules get
the `no HTTP server` rejection).

Rules stay on the board until `clear`, a `reset`, or a reflash. If the board
rejects a line, the job fails with `rule_rejected`; `result.line` says which line
and `result.applied` how many lines before it took effect. Lines after it aren't
sent.

## Failure codes

`error.code` on a job that didn't pass:

| Code | State | Meaning |
|---|---|---|
| `rule_rejected` | failed | The firmware rejected a rule line (`result.line`, `result.error`) |
| `no_reply` | failed | No VT reply in time: hung firmware or a build without `VT_CONTROL` |
| `device_error` | failed | The firmware answered `err` to `clear`, `ap-start` or `prov-start` |
| `unknown_device` | (HTTP 404 on `POST /jobs`) | `device=` matched nothing: no sensor attached, or an unknown MAC/port |
| `wrong_transport` | failed | `ble-advertise` on a soft-AP build: use `broadcast-ssid` |
| `unsupported` | failed | `ble-advertise` on firmware without `prov-start` (`unknown command`) |
| `not_ready` | failed | No `@vt - ready` after a reset or flash |
| `bad_request` | error | `script` has no rule lines |
| `wrong_plugin` | (HTTP 404 on `POST /jobs`) | `device=hub`/`server`: belongs to the esp32-server plugin |
| `bad_firmware` | error | `firmware_dir` has no usable `flasher_args.json` or images |
| `flash_failed`, `reset_failed`, `tool_missing` | error | esptool failed (flashing, or resetting a port that has no RTS pin) or isn't installed |
| `port_closed`, `port_error` | error | The serial port went away |
| `timeout` | timeout | The job's `timeout_ms` ran out |
