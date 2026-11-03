# esp32 plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in VisualTester). `GET /actions` returns the same list with full
parameter schemas. `device` may be omitted when one board is attached.

| Action | Params | Passes when | Default timeout |
|---|---|---|---|
| `flash` | `firmware_dir` (path, required), `erase` (bool, false), `wait_ready` (bool, true), `baud` (int) | esptool succeeds and, with `wait_ready`, the board prints `@vt - ready` (or answers `status`) after reboot | 180 s |
| `reset` | `wait_ready` (bool, true) | EN is pulsed and the board prints `@vt - ready` | 30 s |
| `wait-ready` | `require_ip` (bool, false) | The board answers `status` (with a non-empty `ip` if asked) | 30 s |
| `status` | — | The board answers `status`; `result.status` is the status object | 10 s |
| `scenario` | `name` (enum, required), `celsius`, `err`, `status` | Every step gets `ok` and the board's status matches the scenario's `expect` | 30 s |
| `set-temperature` | `celsius` (float, required) | The board reports `temp: fixed` at that value | 10 s |
| `command` | `line` (string, required) | The raw VT command gets `ok`; `result.reply` is its payload | 10 s |
| `serial-send` | `line` (string, required) | The line was written | 5 s |
| `serial-expect` | `pattern` (regex, required), `since` (`job` or `recent`) | A console line matches; `result.groups` holds the captures | 30 s |
| `http` | `path` (required), `method` (`GET`/`POST`), `body` (json), `expect_status` (int, 200), `expect_json` (object) | The board's own HTTP server (at its STA IP) answers as expected | 15 s |

## Scenarios

From `scenarios.json`. Each one starts from `reset-faults`, so scenarios don't
stack; `happy` clears everything.

| Name | Board behaviour | Params |
|---|---|---|
| `happy` | Normal readings (dummy 19.0/20.1 °C in test builds), everything works | — |
| `fixed-temperature` | Every reading is `celsius` | `celsius` (default 21.5) |
| `sensor-fail` | Readings are `ok:false`, `err` set | `err` (default 1) |
| `http-error` | HTTP endpoints answer `status` | `status` 400–599 (default 500) |
| `http-hang` | HTTP endpoints never answer | — |
| `setup-reject` | `POST /setup` fails, nothing stored | — |
| `offline` | Station leaves the home network and stays off | — |
| `mqtt-silent` | No MQTT publishes; HTTP still works | — |
| `unprovisioned` | Provisioned Wi-Fi credentials erased, board reboots | — |

## Failure codes

`error.code` on a job that didn't pass:

| Code | State | Meaning |
|---|---|---|
| `no_reply` | failed | No VT reply in time: hung firmware or a build without `VT_CONTROL` |
| `device_error` | failed | The firmware answered `err` |
| `unexpected_response` | failed | The board answered but isn't in the expected state |
| `not_ready` | failed | No `@vt - ready` after a reset |
| `no_ip`, `http_unreachable`, `unexpected_status` | failed | `http` action checks |
| `bad_firmware` | error | `firmware_dir` has no usable `flasher_args.json` or images |
| `flash_failed`, `tool_missing` | error | esptool failed or isn't installed |
| `port_closed`, `port_error` | error | The serial port went away |
| `timeout` | timeout | The job's `timeout_ms` ran out |
