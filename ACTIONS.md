# esp32 plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in VisualTester). `GET /actions` returns the same list with full
parameter schemas. `device` may be omitted when one board is attached.

| Action | Params | Passes when | Default timeout |
|---|---|---|---|
| `flash` | `firmware_dir` (path, required), `erase` (bool, false), `wait_ready` (bool, true), `baud` (int) | esptool succeeds and, with `wait_ready`, the board prints `@vt - ready` (or answers `status`) after reboot | 180 s |
| `reset` | `wait_ready` (bool, true) | EN is pulsed and the board prints `@vt - ready`. A reboot clears the expectations | 30 s |
| `expect` | `script` (string, required; the test's inline block) | The board accepted every rule line | 30 s |
| `clear` | — | The board dropped every expectation | 10 s |

## `expect`

The test's inline block, one rule per line, is the whole description of how the
board answers the app:

```
PLUGIN: esp32 expect
  # the app sees a broken sensor, and provisioning fails
  GET /temperature.json -> 200 {"device":"{device}","temperature_c":0,"ok":false,"err":1,"ip":"{ip}"}
  POST /setup -> 500
END
```

The plugin doesn't interpret the rules. It drops blank and `#` lines, sends
`clear`, then sends each line as `rule <line>` over the VT control protocol. The
rule language belongs to the firmware: `docs/vt-control.md` in TemperatureSensor
(HTTP responses with a status and body, `hang`, `delay`, and `wifi`/`mqtt`
directives).

Each `expect` **replaces** the previous expectations, so a block always states the
complete behaviour. If the board rejects a line, the job fails with
`rule_rejected`, `result.line` says which line, and the board is cleared again so
no half-loaded script stays active.

## Failure codes

`error.code` on a job that didn't pass:

| Code | State | Meaning |
|---|---|---|
| `rule_rejected` | failed | The firmware rejected a rule line (`result.line`, `result.error`) |
| `no_reply` | failed | No VT reply in time: hung firmware or a build without `VT_CONTROL` |
| `device_error` | failed | The firmware answered `err` to `clear` |
| `not_ready` | failed | No `@vt - ready` after a reset or flash |
| `bad_request` | error | `script` has no rule lines |
| `bad_firmware` | error | `firmware_dir` has no usable `flasher_args.json` or images |
| `flash_failed`, `tool_missing` | error | esptool failed or isn't installed |
| `port_closed`, `port_error` | error | The serial port went away |
| `timeout` | timeout | The job's `timeout_ms` ran out |
