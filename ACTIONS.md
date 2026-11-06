# esp32 plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in VisualTester). `GET /actions` returns the same list with full
parameter schemas.

## Devices and roles

One plugin drives both kinds of board (TemperatureSensor-Hub
`docs/hub-protocol.md`, sections 6 and 7). Each board's `status` / `@vt - ready`
line says `"role":"sensor"` or `"role":"hub"`; firmware from before roles (no
`role` field), and boards that don't answer VT control, count as sensors.

`device=` in a test step (the job's `device`) is one of:

| Value | Resolves to |
|---|---|
| omitted, or `sensor` | The one attached sensor board. Fails (`unknown_device`, 404) when none is attached, and (`bad_request`, 400) when there are several: then pass the MAC |
| `hub` | The attached `role: hub` board; when none is attached, the plugin's **fake hub** (see below), depending on `ESP32_FAKE_HUB` |
| a MAC, or a serial port | That board. `tshub-fake` (or the fake hub's MAC `02:00:00:fa:4e:01`) is the fake hub |

`GET /devices` lists every board with `details.role`, plus an entry with id
`tshub-fake` (`details.fake: true`, `details.url`, `details.mdns`) while the fake
hub is running. Sensor entries also carry the firmware's `hub`, `report` and
`last_report` status fields; hub entries carry `sensors` and `time_synced`.

## The fake hub

When no hub board is attached, the plugin runs a hub in software so app tests
don't need a second board. It implements hub-protocol.md sections 1-4 and the
hub's rule language from section 6, on the plugin's host:

- **HTTP** on `0.0.0.0:8092` (`ESP32_FAKE_HUB_PORT`, `ESP32_FAKE_HUB_BIND`):
  `POST /report`, `GET /info.json`, `GET /devices`, `GET /devices/<mac>`,
  `GET /devices/<mac>/history`, `DELETE /devices/<mac>`. Phones and real sensors
  on the LAN must be able to reach this port.
- **mDNS:** `_tshub._tcp`, instance and hostname `tshub-fake`, TXT `proto=1`,
  `mac=02:00:00:fa:4e:01`, `fw=<plugin version>`, on one IPv4 address: the
  default-route interface's, or `ESP32_FAKE_HUB_ADDR`. Needs python `zeroconf`
  (in the flake; `pip install zeroconf` otherwise). If advertising fails, HTTP
  keeps working and `details.mdns` in `/devices` says why.
- Registry of 16 sensors keyed by MAC (evicts the one offline longest; 507 when
  all 16 are online), per-minute history (180 points, last good reading of each
  minute), `online` while the last report is younger than
  `max(3 × interval_s, 15 s)`, epoch-ms times from the host clock
  (`time_synced` is always true). Everything lives in memory: restarting the
  plugin empties it.

`ESP32_FAKE_HUB` chooses when `device=hub` uses it:

| Value | Fake hub |
|---|---|
| `auto` (default) | Runs while no `role: hub` board is attached. When a hub board appears it stops serving and stops advertising, so sensors and the app move to the real hub; it starts again when the board goes away |
| `on` | Always runs, and `device=hub` is always the fake (a hub board is still reachable by its MAC) |
| `off` | Never runs; `device=hub` needs a hub board |

Tests don't change between the fake and a real hub.

## Actions

| Action | Params | Passes when | Default timeout |
|---|---|---|---|
| `flash` | `firmware_dir` (path, required), `erase` (bool, false), `wait_ready` (bool, true), `baud` (int) | esptool succeeds and, with `wait_ready`, the board prints `@vt - ready` (or answers `status`) after reboot | 180 s |
| `reset` | `wait_ready` (bool, true) | EN is pulsed and the board prints `@vt - ready`. A reboot drops every rule | 30 s |
| `factory-reset` | — | The board forgets its provisioned Wi-Fi network, reboots, and comes back ready with no rules | 30 s |
| `broadcast-ssid` | `seconds` (int; 0 = until provisioned or reboot; default: the firmware's `PROVISION_TIMEOUT_S`) | The board reports its provisioning SSID (`BJF-<mac>`) is broadcasting; `result.ssid` is the name | 10 s |
| `ble-advertise` | `seconds` (int; 0 = until provisioned or reboot; default: the firmware's `PROVISION_TIMEOUT_S`) | The board answers `prov-start` and reports BLE provisioning is on; `result.name` is its BLE name (`BJF-<mac>`), `result.transport` is `ble`. Needs firmware with `prov-start` | 10 s |
| `expect` | `script` (string, required; the test's inline block) | The board accepted every rule line | 30 s |
| `clear` | — | The board dropped every rule and directive | 10 s |
| `forget` | `mac` (string, required) | Hub only: the hub forgot that sensor (registry and history), like `DELETE /devices/<mac>` | 10 s |

What each action does per device:

| Action | sensor board | hub board | fake hub |
|---|---|---|---|
| `flash`, `reset` | yes | yes | `unsupported` (error) |
| `factory-reset` | forgets Wi-Fi, reboots | erases registry and history, reboots | clears registry, history and rules |
| `ble-advertise`, `broadcast-ssid` | yes | `wrong_role` (error): the hub has no provisioning window | `unsupported` (error) |
| `expect` | relays `rule` lines (directives only, see below) | relays `rule` lines | applies the hub rule language itself |
| `clear` | relays `clear` | relays `clear` | drops every rule |
| `forget` | `wrong_role` (error) | relays `forget <mac>` | forgets the sensor; `device_error` if unknown |

## Starting a test

Start every app test from factory settings, then let the app find the board.
With a hub (hub-protocol.md section 7), reset both, provision the sensor, then
state how the hub answers the app:

```
PLUGIN: esp32 factory-reset device=hub
PLUGIN: esp32 factory-reset device=sensor
PLUGIN: esp32 ble-advertise device=sensor seconds=0
# …the app finds BLE device BJF-<mac> and provisions the sensor; the sensor
# reports to the hub (the fake one when no hub board is attached)…
PLUGIN: esp32 expect device=hub
  GET /devices -> sequence 17 18 19
END
```

Before hubs (one sensor board serving HTTP itself) it was just:

```
PLUGIN: esp32 factory-reset
PLUGIN: esp32 ble-advertise seconds=0
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
PLUGIN: esp32 expect
  # the sensor breaks, and provisioning starts failing
  GET /temperature.json -> 200 {"device":"{device}","temperature_c":0,"ok":false,"err":1,"ip":"{ip}"}
  POST /setup -> 500
END
VERIFY: The temperature card shows a sensor error
PLUGIN: esp32 expect
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
the directives `report on|off` (stop/start `POST /report`, so the hub sees it go
offline), `temp <t1> [<t2> …]` (report these values in turn instead of the
probe; 1-8 values, -55..125), `temp normal`, plus `wifi`/`mqtt on|off`. HTTP
rules go to the hub (`device=hub`), for `/devices`, `/devices/<mac>`,
`/devices/<mac>/history`, `/info.json` and `POST /report` (e.g.
`POST /report -> 500` makes the hub refuse sensors):

```
PLUGIN: esp32 expect device=hub
  GET /devices -> 200 []
END
VERIFY: The dashboard shows no sensors
PLUGIN: esp32 expect device=sensor
  report off
END
```

**Fake hub rule language** (the same as the hub firmware's): `<METHOD> <path> ->
<status> [<body>]`, `hang`, `delay <ms> <status> [<body>]`, `sequence <t1> …`,
`normal`; METHOD `GET`, `POST` or `ANY` (`ANY` also covers `DELETE`), exact path
match, a method rule wins over an `ANY` rule; at most 16 rules and 200-byte
bodies; `{device}` `{mac}` `{ip}` `{ts_ms}` in a body become the hub's values
(`tshub-fake`, its MAC, its advertised IP, now). `sequence` answers with the
hub's normal JSON for that path with **every** `temperature_c` replaced by the
next value (each `/devices` entry gets the same value; such entries also get
`ok: true`, `err: 0`); it keeps the normal status (an unknown MAC is still a
404). `hang` holds the request without an answer until the rule is removed or
replaced, `clear`, or 120 s, then closes the connection. A rule for
`POST /report` replaces the real handler: nothing is stored. The fake hub
rejects sensor directives (`wifi`, `mqtt`, `report`, `temp`).

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
| `device_error` | failed | The firmware answered `err` to `clear`, `ap-start`, `prov-start` or `forget`; or the fake hub doesn't know the MAC given to `forget` |
| `unknown_device` | (HTTP 404 on `POST /jobs`) | `device=` matched nothing: no sensor attached, no hub and `ESP32_FAKE_HUB=off`, or an unknown MAC |
| `wrong_transport` | failed | `ble-advertise` on a soft-AP build: use `broadcast-ssid` |
| `unsupported` | failed | `ble-advertise` on firmware without `prov-start` (`unknown command`) |
| `not_ready` | failed | No `@vt - ready` after a reset or flash |
| `bad_request` | error | `script` has no rule lines; `forget` got a malformed `mac` |
| `unsupported` | error | `flash`, `reset`, `ble-advertise` or `broadcast-ssid` on the fake hub |
| `wrong_role` | error | `ble-advertise`/`broadcast-ssid` on a hub board, or `forget` on a sensor |
| `bad_firmware` | error | `firmware_dir` has no usable `flasher_args.json` or images |
| `flash_failed`, `tool_missing` | error | esptool failed or isn't installed |
| `port_closed`, `port_error` | error | The serial port went away |
| `timeout` | timeout | The job's `timeout_ms` ran out |
