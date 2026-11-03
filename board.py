"""One ESP32 board on a serial port, speaking the VT control protocol.

The board object owns the port: a reader thread keeps it open, splits the
stream into lines, publishes every line as a `serial` event, and routes
`@vt <id> ok|err ...` replies to whoever sent the request. Flashing pauses the
reader so esptool can have the port.

The protocol itself is specified in the firmware repo (TemperatureSensor,
docs/vt-control.md).
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
from collections import deque

import serial  # pyserial

from plugin_server import ActionError, ActionFailed, JobContext

VT_PREFIX = "@vt "
LINE_HISTORY = 2000
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class VtReply:
    def __init__(self, ok: bool, payload):
        self.ok = ok
        self.payload = payload


class Board:
    def __init__(self, port: str, baud: int, events, open_serial=None):
        self.port = port
        self.baud = baud
        self.events = events
        self._open_serial = open_serial or self._default_open
        self._ser = None
        self._lock = threading.RLock()        # guards _ser and writes
        self._cond = threading.Condition()    # lines / replies arrived
        self._lines: deque = deque(maxlen=LINE_HISTORY)  # (seq, monotonic, text)
        self._line_seq = 0
        self._replies: dict[str, VtReply] = {}
        self._ids = itertools.count(1)
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._reader = None
        self.id: str = port               # replaced by the MAC once known
        self.mac: str | None = None
        self.model: str | None = None
        self.status: dict | None = None   # last status object seen
        self.vt: bool | None = None       # firmware answers VT control?
        self.last_ready_seq = 0
        self.error: str | None = None
        self.online = False

    # ───────────── port lifecycle ─────────────

    def _default_open(self):
        s = serial.Serial()
        s.port = self.port
        s.baudrate = self.baud
        s.timeout = 0.2
        # Don't toggle EN/IO0 through the auto-reset circuit just by opening.
        s.dtr = False
        s.rts = False
        s.open()
        return s

    def start(self) -> None:
        self._open()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"serial-{self.port}")
        self._reader.start()

    def _open(self) -> None:
        with self._lock:
            if self._ser is None:
                self._ser = self._open_serial()
                self.online = True
                self.error = None

    def _close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def pause(self) -> None:
        """Release the port (for esptool)."""
        self._paused.set()
        self._close()

    def resume(self) -> None:
        self._open()
        self._paused.clear()

    # ───────────── reading ─────────────

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            with self._lock:
                ser = self._ser
            if ser is None:
                time.sleep(0.05)
                continue
            try:
                chunk = ser.read(512)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if self._paused.is_set() or self._stop.is_set():
                    continue
                self.online = False
                self.error = f"serial read failed: {e}"
                self.events.publish(self.id, "state", {"state": "offline", "error": self.error})
                self._close()
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._on_line(raw.decode("utf-8", "replace").rstrip("\r"))
            if len(buf) > 4096:  # no newline in a long time: flush it as a line
                self._on_line(buf.decode("utf-8", "replace"))
                buf = b""

    def _on_line(self, text: str) -> None:
        clean = _ANSI.sub("", text)
        with self._cond:
            self._line_seq += 1
            seq = self._line_seq
            self._lines.append((seq, time.monotonic(), clean))
            if clean.startswith(VT_PREFIX):
                self._handle_vt(seq, clean[len(VT_PREFIX):])
            self._cond.notify_all()
        self.events.publish(self.id, "serial", clean)

    def _handle_vt(self, seq: int, rest: str) -> None:
        # Called with self._cond held.
        parts = rest.split(" ", 2)
        if len(parts) < 2:
            return
        rid, kind = parts[0], parts[1]
        payload_raw = parts[2] if len(parts) > 2 else ""
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except json.JSONDecodeError:
            payload = {"raw": payload_raw}
        if rid == "-":
            if kind == "ready":
                self.last_ready_seq = seq
                self.vt = True
                self._learn(payload)
            return
        if kind in ("ok", "err"):
            self.vt = True
            self._replies[rid] = VtReply(kind == "ok", payload)
            if kind == "ok" and isinstance(payload, dict) and "proto" in payload:
                self._learn(payload)

    def _learn(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        if "mac" in payload:
            self.status = payload
            if payload.get("mac") and payload["mac"] != self.mac:
                self.mac = payload["mac"]
            if self.mac and self.id == self.port:
                self.id = self.mac  # first time we learn who this board is

    # ───────────── line access for actions ─────────────

    @property
    def line_seq(self) -> int:
        with self._cond:
            return self._line_seq

    # ───────────── writing / VT requests ─────────────

    def write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        with self._lock:
            if self._ser is None:
                raise ActionError(f"{self.port} is not open", code="port_closed")
            try:
                self._ser.write(data)
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                raise ActionError(f"serial write failed: {e}", code="port_error") from None

    def request(self, ctx: JobContext | None, command: str, timeout: float = 3.0) -> VtReply:
        """Send `@vt <id> <command>` and wait for its reply."""
        rid = f"p{next(self._ids)}"
        line = f"@vt {rid} {command}"
        if ctx:
            ctx.log(f"-> {line}")
        self.write_line(line)
        end = time.monotonic() + timeout
        with self._cond:
            while rid not in self._replies:
                if ctx:
                    ctx.check()
                left = end - time.monotonic()
                if ctx:
                    left = min(left, ctx.remaining())
                if left <= 0:
                    if ctx:
                        ctx.check()
                    raise ActionFailed(f"no reply to {command!r} within {timeout:.1f}s "
                                       "(firmware without VT_CONTROL?)", code="no_reply")
                self._cond.wait(min(left, 0.25))
            reply = self._replies.pop(rid)
        if ctx:
            ctx.log(f"<- @vt {rid} {'ok' if reply.ok else 'err'} {json.dumps(reply.payload)}")
        return reply

    def command(self, ctx: JobContext | None, command: str, timeout: float = 3.0):
        """Like request(), but an `err` reply is a failed action."""
        reply = self.request(ctx, command, timeout)
        if not reply.ok:
            msg = reply.payload.get("error") if isinstance(reply.payload, dict) else reply.payload
            raise ActionFailed(f"device rejected {command!r}: {msg}", code="device_error",
                               result=reply.payload)
        return reply.payload

    def probe(self, timeout: float = 1.5) -> bool:
        """Try a `status` without a job (discovery). Sets mac/status on success."""
        try:
            reply = self.request(None, "status", timeout)
        except (ActionFailed, ActionError):
            self.vt = False if self.vt is None else self.vt
            return False
        return reply.ok

    # ───────────── reset ─────────────

    def hard_reset(self) -> None:
        """Pulse EN through the USB-UART's RTS line (standard dev-board wiring)."""
        with self._lock:
            if self._ser is None:
                raise ActionError(f"{self.port} is not open", code="port_closed")
            self._ser.dtr = False
            self._ser.rts = True
            time.sleep(0.12)
            self._ser.rts = False

    # ───────────── description ─────────────

    def to_json(self, busy: bool) -> dict:
        if not self.online:
            state = "offline"
        elif self.error:
            state = "error"
        elif busy:
            state = "busy"
        else:
            state = "ready"
        details = {"port": self.port, "vt_control": self.vt}
        if self.status:
            for k in ("device", "ip", "ssid", "ap_ssid"):
                if k in self.status:
                    details[k] = self.status[k]
        if self.error:
            details["error"] = self.error
        return {"id": self.id, "kind": "esp32", "model": self.model, "state": state,
                "details": details}
