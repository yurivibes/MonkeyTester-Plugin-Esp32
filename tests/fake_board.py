"""An in-memory stand-in for a serial port with VT-control firmware behind it.

FakeSerial looks enough like pyserial's Serial for board.Board; the firmware side
answers the VT control protocol (TemperatureSensor docs/vt-control.md) and prints
the `@vt - ready` line on boot.
"""

import json
import threading
import time


class FakeFirmware:
    def __init__(self, mac="7c:df:a1:a4:8f:44", vt=True, boot_delay=0.05,
                 ready_on_boot=True):
        self.mac = mac
        self.vt = vt
        self.boot_delay = boot_delay
        self.ready_on_boot = ready_on_boot
        self.port = None  # FakeSerial currently attached
        self.received = []
        self.reboots = 0
        self.silent = False  # True: ignore all requests (hung firmware)
        self._reset_state()

    def _reset_state(self):
        self.state = {"temp": "auto", "temp_c": 19.0, "sensor": "ok", "http": "normal",
                      "http_status": 200, "setup": "accept", "sta": "on", "mqtt": "on"}

    def status(self):
        s = {"proto": 1, "device": "ts-" + self.mac.replace(":", "")[4:], "mac": self.mac,
             "ip": "", "ssid": "", "ap_ssid": "BJF-" + self.mac.replace(":", "")[6:],
             "ap_on": True}
        s.update(self.state)
        return s

    # -- device output --
    def emit(self, line):
        if self.port is not None:
            self.port.feed((line + "\n").encode())

    def boot(self):
        self.reboots += 1
        self._reset_state()

        def run():
            time.sleep(self.boot_delay)
            self.emit("ESP-ROM:esp32c3-api1-20210207")
            self.emit("\x1b[0;32mI (312) temp-sensor: test mode enabled\x1b[0m")
            if self.vt and self.ready_on_boot:
                self.emit("@vt - ready " + json.dumps(self.status()))
        threading.Thread(target=run, daemon=True).start()

    # -- device input --
    def on_line(self, line):
        self.received.append(line)
        if not self.vt or self.silent or not line.startswith("@vt "):
            return
        parts = line[4:].split()
        if len(parts) < 2:
            return
        rid, cmd, args = parts[0], parts[1], parts[2:]
        try:
            payload = self.handle(cmd, args)
        except ValueError as e:
            self.emit(f"@vt {rid} err " + json.dumps({"error": str(e)}))
            return
        self.emit(f"@vt {rid} ok " + json.dumps(payload))
        if payload.get("rebooting"):
            self.boot()

    def handle(self, cmd, args):
        st = self.state
        if cmd == "ping":
            return {"proto": 1}
        if cmd == "status":
            return self.status()
        if cmd == "reset-faults":
            self._reset_state()
        elif cmd == "temp":
            if args == ["auto"]:
                st.update(temp="auto", temp_c=19.0)
            else:
                st.update(temp="fixed", temp_c=float(args[0]))
        elif cmd == "sensor":
            st["sensor"] = {"ok": "ok", "fail": "fail"}[args[0]]
        elif cmd == "http":
            if args[0] == "normal":
                st.update(http="normal", http_status=200)
            elif args[0] == "error":
                code = int(args[1])
                if not 400 <= code <= 599:
                    raise ValueError("code must be 400-599")
                st.update(http="error", http_status=code)
            elif args[0] == "hang":
                st["http"] = "hang"
            else:
                raise ValueError("bad http mode")
        elif cmd == "setup":
            st["setup"] = args[0]
        elif cmd == "sta":
            st["sta"] = args[0]
        elif cmd == "mqtt":
            st["mqtt"] = args[0]
        elif cmd in ("reboot", "factory-reset"):
            return {"rebooting": True}
        else:
            raise ValueError(f"unknown command {cmd}")
        return self.status()


class FakeSerial:
    def __init__(self, firmware: FakeFirmware, port="/dev/fake0"):
        self.fw = firmware
        self.port = port
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._in = b""
        self.is_open = True
        self._rts = False
        self.dtr = False
        firmware.port = self

    # firmware -> host
    def feed(self, data: bytes):
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    def read(self, n):
        with self._cond:
            if not self.is_open:
                raise OSError("port closed")
            if not self._buf:
                self._cond.wait(0.1)
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    # host -> firmware
    def write(self, data: bytes):
        self._in += data
        while b"\n" in self._in:
            line, self._in = self._in.split(b"\n", 1)
            self.fw.on_line(line.decode())
        return len(data)

    def flush(self):
        pass

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        if self._rts and not v:  # EN released: the chip boots
            self.fw.boot()
        self._rts = v

    def close(self):
        with self._cond:
            self.is_open = False
            self._cond.notify_all()
        if self.fw.port is self:
            self.fw.port = None
