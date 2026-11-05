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
                 ready_on_boot=True, transport="softap"):
        self.mac = mac
        self.vt = vt
        # "softap" or "ble": the build's provisioning transport. None: firmware
        # from before prov-start (no transport/prov_on/ble_name in its status).
        self.transport = transport
        self.boot_delay = boot_delay
        self.ready_on_boot = ready_on_boot
        self.port = None  # FakeSerial currently attached
        self.received = []
        self.reboots = 0
        self.silent = False  # True: ignore all requests (hung firmware)
        self._reset_state()

    provisioned_ssid = "External"
    ap_on = False
    ap_seconds = None
    prov_on = False

    def _reset_state(self):
        self.rules = {}  # (method, path) -> response text
        self.wifi = "on"
        self.mqtt = "on"

    def status(self):
        name = "BJF-" + self.mac.replace(":", "")[6:]
        st = {"proto": 2, "device": "ts-" + self.mac.replace(":", "")[4:], "mac": self.mac,
              "ip": "", "ssid": self.provisioned_ssid or "MyAccessPoint",
              "ap_ssid": name, "ap_on": self.ap_on, "rules": len(self.rules), "wifi": self.wifi, "mqtt": self.mqtt}
        if self.transport is not None:
            st.update(transport=self.transport, prov_on=self.prov_on, ble_name=name)
        return st

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

    def add_rule(self, line):
        """A small subset of the firmware's rule parser (docs/vt-control.md)."""
        words = line.split()
        if len(words) == 2 and words[0] in ("wifi", "mqtt") and words[1] in ("on", "off"):
            setattr(self, words[0], words[1])
            return
        if len(words) < 4 or words[2] != "->":
            raise ValueError("expected <METHOD> <path> -> <response>")
        method, path = words[0], words[1]
        if method not in ("GET", "POST", "ANY"):
            raise ValueError("bad method")
        resp = line.split("->", 1)[1].strip()
        head = resp.split()[0]
        if head == "sequence":
            values = resp.split()[1:]
            if not 1 <= len(values) <= 8:
                raise ValueError("bad sequence")
            for v in values:
                try:
                    t = float(v)
                except ValueError:
                    raise ValueError("bad sequence value")
                if not -55 <= t <= 125:
                    raise ValueError("bad sequence value")
        elif head not in ("hang", "delay", "normal"):
            if not head.isdigit() or not 100 <= int(head) <= 599:
                raise ValueError("bad status")
        if head == "normal":
            self.rules.pop((method, path), None)
            return
        if (method, path) not in self.rules and len(self.rules) >= 16:
            raise ValueError("too many rules")
        self.rules[(method, path)] = resp

    def handle(self, cmd, args):
        if cmd == "ping":
            return {"proto": 2}
        if cmd == "status":
            return self.status()
        if cmd == "clear":
            self._reset_state()
            return {"rules": 0}
        if cmd == "rule":
            self.add_rule(" ".join(args))
            return {"rules": len(self.rules)}
        if cmd in ("reboot", "factory-reset"):
            if cmd == "factory-reset":
                self.provisioned_ssid = ""
            return {"rebooting": True}
        if cmd == "ap-start" or (cmd == "prov-start" and self.transport is not None):
            if args and (not args[0].isdigit() or int(args[0]) > 86400):
                raise ValueError("bad seconds (0-86400)")
            self.prov_on = True
            self.ap_on = self.transport != "ble"  # BLE builds have no soft-AP
            self.ap_seconds = int(args[0]) if args else None
            return self.status()
        raise ValueError("unknown command")


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
