# esp32 plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in VisualTester). `GET /actions` returns the same list with full
parameter schemas. `device` may be omitted when one board is attached.

| Action | Params | Passes when | Default timeout |
|---|---|---|---|
| `flash` | `firmware_dir` (path, required), `erase` (bool, false), `wait_ready` (bool, true), `baud` (int) | esptool succeeds and, with `wait_ready`, the board prints `@vt - ready` (or answers `status`) after reboot | 180 s |
| `reset` | `wait_ready` (bool, true) | EN is pulsed and the board prints `@vt - ready`. A reboot drops every rule | 30 s |
| `expect` | `script` (string, required; the test's inline block) | The board accepted every rule line | 30 s |
| `clear` | — | The board dropped every rule and directive | 10 s |

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

The plugin doesn't interpret the rules. It drops blank and `#` lines and sends
each line as `rule <line>` over the VT control protocol, nothing else. The rule
language belongs to the firmware: `docs/vt-control.md` in TemperatureSensor
(HTTP responses with a status and body, `hang`, `delay`, `normal` to undo one
rule, and `wifi`/`mqtt` directives). A later rule for the same method and path
replaces the earlier one.

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
| `device_error` | failed | The firmware answered `err` to `clear` |
| `not_ready` | failed | No `@vt - ready` after a reset or flash |
| `bad_request` | error | `script` has no rule lines |
| `bad_firmware` | error | `firmware_dir` has no usable `flasher_args.json` or images |
| `flash_failed`, `tool_missing` | error | esptool failed or isn't installed |
| `port_closed`, `port_error` | error | The serial port went away |
| `timeout` | timeout | The job's `timeout_ms` ran out |
