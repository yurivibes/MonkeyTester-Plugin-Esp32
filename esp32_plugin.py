#!/usr/bin/env python3
"""VisualTester-Esp32: the `esp32` device plugin for VisualTester.

Owns the ESP32 boards attached to this host (Linux or macOS) and exposes them
through the VisualTester Device Plugin API (PLUGIN_API.md in the VisualTester
repo; generic part in plugin_server.py). Actions are listed in ACTIONS.md.

Environment:
  PLUGIN_BIND        listen address           (default 0.0.0.0)
  PLUGIN_PORT        listen port              (default 8091)
  PLUGIN_TOKEN       if set, required as X-Plugin-Token on every request
  ESP32_PORTS        comma-separated serial ports or globs to use
                     (default: USB-UART ports for this OS, see DEFAULT_GLOBS)
  ESP32_BAUD         console baud rate        (default 115200)
  ESP32_FLASH_BAUD   esptool baud rate        (default 460800)
  ESPTOOL            esptool command          (default: `esptool` on PATH, else
                     "<this python> -m esptool")
  ESP32_FIRMWARE_ROOT  relative firmware_dir values resolve against this
                     (default: the current directory)
  ESP32_READY_TIMEOUT_S  how long to wait for `@vt - ready` after a reset (default 20)
  PLUGIN_UPLOAD_DIR  where POST /uploads unpacks zips (default: <tmp>/vt-plugin-uploads)
  ESP32_FAKE_HUB     auto | on | off: when `device=hub` uses the software fake hub
                     (default auto: only while no `role: hub` board is attached)
  ESP32_FAKE_HUB_PORT  fake hub HTTP port   (default 8092)
  ESP32_FAKE_HUB_BIND  fake hub listen address (default 0.0.0.0)
  ESP32_FAKE_HUB_ADDR  IPv4 the fake hub advertises over mDNS and reports as its
                     `ip` (default: the address of the default-route interface)
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

import plugin_server as ps
from board import Board
from fake_hub import FAKE_HUB_ID, MAC_RE, FakeHub
from plugin_server import (Action, ActionError, ActionFailed, HttpError, JobContext,
                           Param, Plugin, PluginServer, Uploads)

VERSION = "0.1.0"

DEFAULT_GLOBS = {
    "linux": ["/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"],
    "darwin": ["/dev/cu.usbserial-*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
               "/dev/cu.usbmodem*"],
}
RESCAN_S = 5.0
READY_TIMEOUT_S = float(os.environ.get("ESP32_READY_TIMEOUT_S", "20"))
FAKE_HUB_MODES = ("auto", "on", "off")
IDENTIFY_WAIT_S = 3.0


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ───────────────────────────── flashing ─────────────────────────────


def find_flasher_args(firmware_dir: str) -> tuple[str, dict]:
    """Locate flasher_args.json in an ESP-IDF build dir (or a Nix `result/`)."""
    for cand in (firmware_dir, os.path.join(firmware_dir, "build")):
        p = os.path.join(cand, "flasher_args.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    return cand, json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise ActionError(f"can't read {p}: {e}", code="bad_firmware") from None
    raise ActionError(f"no flasher_args.json in {firmware_dir} or {firmware_dir}/build "
                      "(pass an ESP-IDF build directory)", code="bad_firmware")


def esptool_command() -> list:
    cmd = os.environ.get("ESPTOOL")
    if cmd:
        return shlex.split(cmd)
    for exe in ("esptool", "esptool.py"):
        found = shutil.which(exe)
        if found:
            return [found]
    return [sys.executable, "-m", "esptool"]


def build_flash_argv(port: str, build_dir: str, args: dict, baud: int, erase: bool) -> list:
    extra = args.get("extra_esptool_args") or {}
    files = args.get("flash_files") or {}
    if not files:
        raise ActionError("flasher_args.json lists no flash_files", code="bad_firmware")
    argv = esptool_command()
    if extra.get("stub") is False:
        argv.append("--no-stub")
    argv += ["--port", port, "--baud", str(baud)]
    if extra.get("chip"):
        argv += ["--chip", extra["chip"]]
    argv += ["--before", extra.get("before", "default_reset"),
             "--after", extra.get("after", "hard_reset"),
             "write_flash"]
    if erase:
        argv.append("--erase-all")
    argv += list(args.get("write_flash_args") or [])
    for addr, rel in sorted(files.items(), key=lambda kv: int(kv[0], 0)):
        path = os.path.join(build_dir, rel)
        if not os.path.isfile(path):
            raise ActionError(f"missing image {path}", code="bad_firmware")
        argv += [addr, path]
    return argv


def run_tool(ctx: JobContext, argv: list) -> tuple[int, str]:
    ctx.log("$ " + " ".join(shlex.quote(a) for a in argv))
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except FileNotFoundError as e:
        raise ActionError(f"can't run {argv[0]}: {e}", code="tool_missing") from None
    out = []

    def pump():
        for line in proc.stdout:
            line = line.rstrip()
            out.append(line)
            # esptool's progress lines are noisy; keep the transcript readable.
            if not re.match(r"^Writing at 0x[0-9a-f]+", line):
                ctx.log(line)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        while proc.poll() is None:
            try:
                ctx.check()
            except BaseException:
                proc.kill()
                raise
            time.sleep(0.1)
    finally:
        t.join(2)
        proc.stdout.close()
    return proc.returncode, "\n".join(out)


# ───────────────────────────── the plugin ─────────────────────────────


class Esp32Plugin(Plugin):
    name = "esp32"
    version = VERSION
    capabilities = ["events", "uploads"]

    def __init__(self, ports=None, baud=None, flash_baud=None,
                 open_serial=None, firmware_root=None, rescan=True,
                 fake_hub_mode=None, fake_hub_port=None, fake_hub_bind=None,
                 fake_hub_address=None, mdns_advertiser=None, log=None):
        super().__init__()
        if os.environ.get("PLUGIN_UPLOAD_DIR"):
            self.uploads = Uploads(os.environ["PLUGIN_UPLOAD_DIR"])
        self.port_specs = ports if ports is not None else self._default_port_specs()
        self.baud = baud or _env_int("ESP32_BAUD", 115200)
        self.flash_baud = flash_baud or _env_int("ESP32_FLASH_BAUD", 460800)
        self.firmware_root = firmware_root or os.environ.get("ESP32_FIRMWARE_ROOT") or os.getcwd()
        self._open_serial = open_serial
        self._boards: dict[str, Board] = {}  # port -> Board
        self._lock = threading.Lock()
        self._error = None
        self.jobs = None  # set by main() / tests, to report `busy`
        mode = (fake_hub_mode or os.environ.get("ESP32_FAKE_HUB") or "auto").strip().lower()
        if mode not in FAKE_HUB_MODES:
            raise ValueError(f"ESP32_FAKE_HUB must be auto, on or off (got {mode!r})")
        self.fake_hub_mode = mode
        self._fake_lock = threading.Lock()
        self.fake_hub: FakeHub | None = None
        if mode != "off":
            self.fake_hub = FakeHub(
                port=fake_hub_port if fake_hub_port is not None
                else _env_int("ESP32_FAKE_HUB_PORT", 8092),
                bind=fake_hub_bind or os.environ.get("ESP32_FAKE_HUB_BIND") or "0.0.0.0",
                address=fake_hub_address or os.environ.get("ESP32_FAKE_HUB_ADDR") or None,
                fw=VERSION, advertiser_factory=mdns_advertiser,
                log=log or (lambda m: print(m, flush=True)))
        self._actions = self._build_actions()
        self._scanned = False  # no fake-hub decision before the first scan found every board
        self.rescan()
        self._scanned = True
        self.update_fake_hub()
        if rescan:
            threading.Thread(target=self._rescan_loop, daemon=True).start()

    # ── discovery ──────────────────────────────────────────────

    @staticmethod
    def _default_port_specs() -> list:
        env = os.environ.get("ESP32_PORTS")
        if env:
            return [p.strip() for p in env.split(",") if p.strip()]
        return DEFAULT_GLOBS.get(platform.system().lower(), DEFAULT_GLOBS["linux"])

    def _candidate_ports(self) -> list:
        seen_real, ports = set(), []
        for spec in self.port_specs:
            matches = sorted(glob.glob(spec)) if any(c in spec for c in "*?[") else [spec]
            for p in matches:
                if not os.path.exists(p):
                    continue
                real = os.path.realpath(p)
                if real in seen_real:  # /dev/serial/by-id/x and /dev/ttyUSB0 are one board
                    continue
                seen_real.add(real)
                ports.append(p)
        return ports

    def rescan(self) -> None:
        ports = self._candidate_ports()
        with self._lock:
            known = dict(self._boards)
        for port, b in known.items():
            if not b.online and not b._paused.is_set():
                b.stop()
                with self._lock:
                    self._boards.pop(port, None)
                self.events.publish(b.id, "state", {"state": "removed", "port": port})
        for port in ports:
            with self._lock:
                known_board = self._boards.get(port)
            if known_board is not None:
                # Not identified yet (booting, or flashed with non-VT firmware since)?
                # Ask again, unless a job owns the board.
                if (known_board.mac is None and known_board.online
                        and not known_board._paused.is_set()
                        and not (self.jobs and self.jobs.is_busy(known_board.id))):
                    threading.Thread(target=self._identify, args=(known_board,),
                                     daemon=True).start()
                continue
            b = Board(port, self.baud, self.events, open_serial=self._open_serial)
            try:
                b.start()
            except Exception as e:  # noqa: BLE001 - busy/permission: report, retry later
                self._error = f"{port}: {e}"
                continue
            with self._lock:
                self._boards[port] = b
            threading.Thread(target=self._identify, args=(b,), daemon=True).start()
        self._error = None if ports or not self.port_specs else self._error

    def _identify(self, b: Board) -> None:
        if b.probe():
            if b.mac:
                b.id = b.mac
            self.events.publish(b.id, "state", {"state": "ready", "port": b.port,
                                                "vt_control": True, "role": b.role})
        else:
            self.events.publish(b.id, "state", {"state": "ready", "port": b.port,
                                                "vt_control": False})
        self.update_fake_hub()

    # ── the fake hub ──────────────────────────────────────────

    def _identifying(self) -> list:
        return [b for b in self.boards() if b.online and b.vt is None]

    def update_fake_hub(self) -> None:
        """Start/stop the fake hub. `on`: always running. `auto`: running while no
        `role: hub` board is attached (decided once every board has answered or
        failed its status probe), so sensors and the app move to a real hub as soon
        as one appears. `off`: never."""
        hub = self.fake_hub
        if hub is None or not self._scanned:
            return
        with self._fake_lock:
            if self.fake_hub_mode == "on":
                want = True
            else:
                if self._identifying():
                    return
                want = not any(b.role == "hub" for b in self.boards() if b.online)
            if want and not hub.active:
                try:
                    hub.start()
                except OSError:
                    self.events.publish(hub.id, "state", {"state": "error", "error": hub.error})
                    return
                self.events.publish(hub.id, "state", {"state": "ready", "role": "hub",
                                                      "fake": True, "url": hub.url})
            elif not want and hub.active:
                hub.stop()
                self.events.publish(hub.id, "state", {"state": "removed", "role": "hub",
                                                      "fake": True,
                                                      "reason": "a hub board is attached"})

    def _fake_hub_json(self) -> dict:
        hub = self.fake_hub
        busy = bool(self.jobs and self.jobs.is_busy(hub.id))
        st = hub.status()
        return {"id": hub.id, "kind": "esp32", "model": "fake hub",
                "state": "busy" if busy else "ready",
                "details": {"role": "hub", "fake": True, "mac": hub.mac, "url": hub.url,
                            "device": st["device"], "ip": st["ip"], "port": st["port"],
                            "sensors": st["sensors"], "rules": st["rules"],
                            "mdns": st["mdns"]}}

    def close(self) -> None:
        if self.fake_hub is not None:
            self.fake_hub.stop()

    def _rescan_loop(self) -> None:
        while True:
            time.sleep(RESCAN_S)
            try:
                self.rescan()
                self.update_fake_hub()
            except Exception as e:  # noqa: BLE001
                self._error = f"rescan failed: {e}"

    def boards(self) -> list:
        with self._lock:
            return list(self._boards.values())

    # ── Plugin interface ──────────────────────────────────────

    def devices(self) -> list:
        out = []
        for b in self.boards():
            busy = bool(self.jobs and self.jobs.is_busy(b.id))
            out.append(b.to_json(busy))
        if self.fake_hub is not None and self.fake_hub.active:
            out.append(self._fake_hub_json())
        return out

    def device_error(self):
        if self._error:
            return self._error
        if self.fake_hub is not None and self.fake_hub.error:
            return f"fake hub: {self.fake_hub.error}"
        return None

    def _wait_identified(self) -> None:
        end = time.monotonic() + IDENTIFY_WAIT_S
        while self._identifying() and time.monotonic() < end:
            time.sleep(0.05)

    def resolve_device(self, ref):
        """`device=` is a MAC (or serial port), `sensor`, or `hub`; omitted = `sensor`."""
        if isinstance(ref, str):
            ref = ref.strip()
        if ref in (None, "", "sensor"):
            return self._resolve_sensor()
        if ref == "hub":
            return self._resolve_hub()
        hub = self.fake_hub
        if hub is not None and isinstance(ref, str) and ref.lower() in (hub.id, hub.mac):
            if not hub.active:
                raise HttpError(404, "unknown_device",
                                "the fake hub isn't running (a hub board is attached, or "
                                f"it failed to start: {hub.error})")
            return hub.id, hub
        for b in self.boards():
            if ref in (b.id, b.mac, b.port) or os.path.realpath(str(ref)) == os.path.realpath(b.port):
                return b.id, b
        raise HttpError(404, "unknown_device", f"no board {ref!r}")

    def _resolve_sensor(self):
        self._wait_identified()
        boards = self.boards()
        sensors = [b for b in boards if b.role == "sensor"]
        if len(sensors) == 1:
            return sensors[0].id, sensors[0]
        if not sensors:
            others = f" (attached: {', '.join(f'{b.id} [{b.role}]' for b in boards)})" \
                if boards else ""
            raise HttpError(404, "unknown_device", f"no sensor board attached{others}")
        raise HttpError(400, "bad_request",
                        f"{len(sensors)} sensor boards attached; pass device=<mac> "
                        f"({', '.join(b.id for b in sensors)})")

    def _resolve_hub(self):
        hub = self.fake_hub
        if self.fake_hub_mode == "on":
            self.update_fake_hub()
            if not hub.active:
                raise HttpError(404, "unknown_device", f"the fake hub isn't running: {hub.error}")
            return hub.id, hub
        self._wait_identified()
        hubs = [b for b in self.boards() if b.role == "hub"]
        if len(hubs) == 1:
            return hubs[0].id, hubs[0]
        if len(hubs) > 1:
            raise HttpError(400, "bad_request",
                            f"{len(hubs)} hub boards attached; pass device=<mac> "
                            f"({', '.join(b.id for b in hubs)})")
        if hub is None:
            raise HttpError(404, "unknown_device",
                            "no hub board attached (and ESP32_FAKE_HUB=off)")
        self.update_fake_hub()
        if not hub.active:
            raise HttpError(404, "unknown_device", f"no hub board attached and the fake hub "
                            f"isn't running: {hub.error}")
        return hub.id, hub

    def resolve_path(self, value: str) -> str:
        if value.startswith("upload:"):
            return self.uploads.resolve(value)
        value = os.path.expanduser(value)
        if not os.path.isabs(value):
            value = os.path.join(self.firmware_root, value)
        return os.path.abspath(value)

    def actions(self) -> dict:
        return self._actions

    # ── actions ───────────────────────────────────────────────

    def _build_actions(self) -> dict:
        acts = [
            Action("flash",
                   "Write a prebuilt ESP-IDF firmware directory (containing flasher_args.json, "
                   "e.g. a Nix `result/` or `result/build`) to the board, then wait for the "
                   "firmware's `@vt - ready` line.",
                   self.act_flash,
                   {"firmware_dir": Param("path", "build directory on the plugin host, "
                                          "or upload:<id>", required=True),
                    "erase": Param("bool", "erase the whole flash first (also clears NVS)",
                                   default=False),
                    "wait_ready": Param("bool", "require the VT ready line after boot",
                                        default=True),
                    "baud": Param("int", "esptool baud rate", default=None)},
                   default_timeout_ms=180_000),
            Action("reset", "Hard-reset the board through EN and wait until it is ready. "
                   "Clears every expectation.",
                   self.act_reset,
                   {"wait_ready": Param("bool", "require the VT ready line", default=True)},
                   default_timeout_ms=30_000),
            Action("factory-reset",
                   "Put the board back to factory settings: forget the provisioned Wi-Fi "
                   "network, drop every rule, reboot, and wait until it is ready. Use it as a "
                   "test's first device step.",
                   self.act_factory_reset, default_timeout_ms=30_000),
            Action("broadcast-ssid",
                   "Start broadcasting the provisioning SSID (BJF-<mac>) so the app can find "
                   "and provision the board, as if its provisioning button had been held.",
                   self.act_broadcast_ssid,
                   {"seconds": Param("int", "how long to broadcast; 0 = until provisioned or "
                                     "reboot; default: the firmware's PROVISION_TIMEOUT_S")},
                   default_timeout_ms=10_000),
            Action("ble-advertise",
                   "Start BLE provisioning: the board advertises as BJF-<mac> so the app can "
                   "find and provision it, as if its provisioning button had been held. Needs "
                   "firmware with `prov-start`; soft-AP builds use broadcast-ssid.",
                   self.act_ble_advertise,
                   {"seconds": Param("int", "how long to advertise; 0 = until provisioned or "
                                     "reboot; default: the firmware's PROVISION_TIMEOUT_S")},
                   default_timeout_ms=10_000),
            Action("expect",
                   "Apply changes to how the device answers the app: each line of `script` is "
                   "sent as a rule, on top of the rules already loaded (state only what "
                   "changes; `<METHOD> <path> -> normal` undoes one rule, `clear` undoes all). "
                   "The rule language is the firmware's (TemperatureSensor "
                   "docs/vt-control.md, hub-protocol.md section 6): HTTP rules go to "
                   "device=hub (the fake hub applies them itself), `report`/`temp`/`wifi` "
                   "directives to the sensor. Blank and # lines are skipped. Stops at the "
                   "first line the device rejects.",
                   self.act_expect,
                   {"script": Param("string", "rule lines, one per line (the test's inline "
                                    "block)", required=True)},
                   default_timeout_ms=30_000),
            Action("clear", "Drop every expectation: the board behaves normally again.",
                   self.act_clear, default_timeout_ms=10_000),
            Action("forget",
                   "Hub only (device=hub): forget a sensor, like DELETE /devices/<mac> "
                   "(registry entry and history). A sensor that still reports registers "
                   "again on its next report.",
                   self.act_forget,
                   {"mac": Param("string", "the sensor's MAC (aa:bb:cc:dd:ee:ff)",
                                 required=True)},
                   default_timeout_ms=10_000),
        ]
        return {a.name: a for a in acts}

    # helpers

    @staticmethod
    def _fake(ctx: JobContext) -> FakeHub | None:
        return ctx.device if isinstance(ctx.device, FakeHub) else None

    def _not_on_fake_hub(self, ctx: JobContext, action: str) -> None:
        if self._fake(ctx):
            raise ActionError(f"{action} is not supported on the fake hub (it is software on "
                              "the plugin host: no firmware, reset line or radio)",
                              code="unsupported")

    @staticmethod
    def _sensor_only(ctx: JobContext, action: str) -> None:
        b = ctx.device
        if isinstance(b, Board) and b.role == "hub":
            raise ActionError(f"{action} is a sensor action; the hub board has no "
                              "provisioning window (its Wi-Fi is set at build time)",
                              code="wrong_role")

    @staticmethod
    def _rule_rejected(n: int, rule: str, msg) -> ActionFailed:
        return ActionFailed(
            f"rule {n} rejected by the board: {msg}: {rule} "
            f"(lines 1-{n - 1} of this block were applied)" if n > 1 else
            f"rule {n} rejected by the board: {msg}: {rule}",
            code="rule_rejected",
            result={"line": n, "rule": rule, "error": msg, "applied": n - 1})

    def _wait_ready_line(self, ctx: JobContext, b: Board, since_seq: int,
                         probe_after: float | None = None) -> dict:
        """Wait for `@vt - ready` after `since_seq`. With `probe_after`, a board that
        answers `status` after that many seconds also counts: the ready line can
        be printed before the port is reopened after esptool's reset."""
        start = time.monotonic()
        end = start + min(READY_TIMEOUT_S, max(ctx.remaining(), 0))
        while b.last_ready_seq <= since_seq:
            ctx.check()
            if probe_after is not None and time.monotonic() - start > probe_after:
                probe_after = None  # once
                reply = None
                try:
                    reply = b.request(ctx, "status", timeout=1.5)
                except ActionFailed:
                    pass
                if reply is not None and reply.ok:
                    ctx.log("ready line not seen (printed before the port reopened); "
                            "board answers status")
                    if b.mac and b.id != b.mac:
                        b.id = b.mac
                    return dict(reply.payload)
            if time.monotonic() > end:
                raise ActionFailed("the board didn't print `@vt - ready` after boot "
                                   "(crashed, or firmware built without VT_CONTROL)",
                                   code="not_ready")
            time.sleep(0.05)
        ctx.log("board is ready")
        if b.mac and b.id != b.mac:
            b.id = b.mac
        return dict(b.status or {})

    def act_flash(self, ctx: JobContext, p: dict):
        self._not_on_fake_hub(ctx, "flash")
        b: Board = ctx.device
        build_dir, args = find_flasher_args(p["firmware_dir"])
        argv = build_flash_argv(b.port, build_dir, args, p["baud"] or self.flash_baud, p["erase"])
        ctx.log(f"flashing {build_dir} -> {b.port}")
        b.pause()
        try:
            rc, out = run_tool(ctx, argv)
        finally:
            seq = b.line_seq
            try:
                b.resume()
            except Exception as e:  # noqa: BLE001
                raise ActionError(f"couldn't reopen {b.port} after flashing: {e}",
                                  code="port_error") from None
        if rc != 0:
            tail = "\n".join(out.splitlines()[-5:])
            raise ActionError(f"esptool exited {rc}: {tail}", code="flash_failed")
        chip = (args.get("extra_esptool_args") or {}).get("chip")
        if chip:
            b.model = chip.upper().replace("ESP32C", "ESP32-C").replace("ESP32S", "ESP32-S")
        result = {"firmware_dir": build_dir, "chip": chip}
        if p["wait_ready"]:
            # esptool resets the board at the end; the port reopened right after.
            result["status"] = self._wait_ready_line(ctx, b, seq, probe_after=3.0)
        return result

    def act_reset(self, ctx: JobContext, p: dict):
        self._not_on_fake_hub(ctx, "reset")
        b: Board = ctx.device
        seq = b.line_seq
        ctx.log("pulsing EN (RTS)")
        b.hard_reset()
        if p["wait_ready"]:
            return {"status": self._wait_ready_line(ctx, b, seq)}
        return {}

    def act_factory_reset(self, ctx: JobContext, p: dict):
        hub = self._fake(ctx)
        if hub:
            hub.factory_reset()
            ctx.log("fake hub: registry, history and rules erased")
            return {"status": hub.status()}
        b: Board = ctx.device
        seq = b.line_seq
        reply = b.command(ctx, "factory-reset")
        if not (isinstance(reply, dict) and reply.get("rebooting")):
            raise ActionFailed(f"unexpected reply to factory-reset: {reply}",
                               result={"reply": reply})
        status = self._wait_ready_line(ctx, b, seq)
        if status.get("rules"):
            raise ActionFailed(f"board still has {status['rules']} rule(s) after reboot",
                               result={"status": status})
        if status.get("role") == "hub":
            ctx.log(f"factory settings: {status.get('sensors')} sensor(s) known")
        else:
            ctx.log(f"factory settings: station ssid {status.get('ssid')!r}, "
                    f"provisioning ssid {status.get('ap_ssid')!r}")
        return {"status": status}

    def act_broadcast_ssid(self, ctx: JobContext, p: dict):
        self._not_on_fake_hub(ctx, "broadcast-ssid")
        self._sensor_only(ctx, "broadcast-ssid")
        b: Board = ctx.device
        cmd = "ap-start" if p["seconds"] is None else f"ap-start {p['seconds']}"
        status = b.command(ctx, cmd)
        if not status.get("ap_on"):
            raise ActionFailed("the board reports its provisioning AP is off",
                               result={"status": status})
        ctx.log(f"broadcasting {status.get('ap_ssid')!r}")
        return {"ssid": status.get("ap_ssid"), "status": status}

    def act_ble_advertise(self, ctx: JobContext, p: dict):
        self._not_on_fake_hub(ctx, "ble-advertise")
        self._sensor_only(ctx, "ble-advertise")
        b: Board = ctx.device
        cmd = "prov-start" if p["seconds"] is None else f"prov-start {p['seconds']}"
        reply = b.request(ctx, cmd)
        if not reply.ok:
            msg = reply.payload.get("error") if isinstance(reply.payload, dict) else reply.payload
            if msg == "unknown command":
                raise ActionFailed("firmware too old for prov-start (flash a build with BLE "
                                   "provisioning)", code="unsupported", result=reply.payload)
            raise ActionFailed(f"device rejected {cmd!r}: {msg}", code="device_error",
                               result=reply.payload)
        status = reply.payload
        if status.get("transport") == "softap":
            raise ActionFailed("board runs a soft-AP build; use broadcast-ssid",
                               code="wrong_transport", result={"status": status})
        if not status.get("prov_on"):
            raise ActionFailed("the board reports provisioning is off",
                               result={"status": status})
        ctx.log(f"advertising {status.get('ble_name')!r} over {status.get('transport')}")
        return {"name": status.get("ble_name"), "transport": status.get("transport"),
                "status": status}

    def act_expect(self, ctx: JobContext, p: dict):
        """Apply a block of changes. Nothing is cleared first: rules accumulate on
        the board until a later block overrides them, `-> normal` removes one, or
        `clear` drops them all."""
        rules = [ln.strip() for ln in p["script"].splitlines()]
        rules = [ln for ln in rules if ln and not ln.startswith("#")]
        if not rules:
            raise ActionError("script has no rules", code="bad_request")
        hub = self._fake(ctx)
        count = None
        for n, rule in enumerate(rules, 1):
            if hub:
                ctx.log(f"fake hub <- rule {rule}")
                try:
                    count = hub.add_rule(rule)
                except ValueError as e:
                    raise self._rule_rejected(n, rule, str(e)) from None
                continue
            reply = ctx.device.request(ctx, "rule " + rule)
            if not reply.ok:
                msg = (reply.payload.get("error") if isinstance(reply.payload, dict)
                       else reply.payload)
                raise self._rule_rejected(n, rule, msg)
            if isinstance(reply.payload, dict):
                count = reply.payload.get("rules", count)
        return {"rules": count, "applied": len(rules)}

    def act_clear(self, ctx: JobContext, p: dict):
        hub = self._fake(ctx)
        if hub:
            hub.clear()
            return {"rules": 0}
        return ctx.device.command(ctx, "clear")

    def act_forget(self, ctx: JobContext, p: dict):
        mac = p["mac"].strip().lower()
        if not MAC_RE.match(mac):
            raise ActionError(f"bad mac {p['mac']!r} (expected aa:bb:cc:dd:ee:ff)",
                              code="bad_request")
        hub = self._fake(ctx)
        if hub:
            if not hub.forget(mac):
                raise ActionFailed(f"the hub doesn't know {mac}", code="device_error",
                                   result={"mac": mac})
            return {"forgotten": mac, "sensors": hub.status()["sensors"]}
        b: Board = ctx.device
        if b.role != "hub":
            raise ActionError("forget is a hub action; use device=hub", code="wrong_role")
        reply = b.command(ctx, f"forget {mac}")
        return {"forgotten": mac, "reply": reply}


def main() -> None:
    plugin = Esp32Plugin()
    server = PluginServer(plugin, os.environ.get("PLUGIN_BIND", "0.0.0.0"),
                          _env_int("PLUGIN_PORT", 8091), os.environ.get("PLUGIN_TOKEN") or None)
    plugin.jobs = server.jobs
    # systemd/launchd stop with SIGTERM: exit through `finally` so mDNS says goodbye.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"VisualTester-Esp32 {VERSION} (contract {ps.CONTRACT} v{ps.CONTRACT_VERSION}) "
          f"on :{server.port}; ports {plugin.port_specs}; fake hub {plugin.fake_hub_mode}",
          flush=True)
    for b in plugin.boards():
        print(f"  board on {b.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        plugin.close()  # unregister the fake hub's mDNS name


if __name__ == "__main__":
    main()
