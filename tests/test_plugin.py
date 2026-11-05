"""Contract + action tests for the esp32 plugin against a fake board.

Run: python3 -m unittest discover tests   (needs pyserial importable)
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import io

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

os.environ.setdefault("ESP32_READY_TIMEOUT_S", "3")
import esp32_plugin  # noqa: E402
from esp32_plugin import Esp32Plugin  # noqa: E402
from fake_board import FakeFirmware, FakeSerial  # noqa: E402
from plugin_server import PluginServer  # noqa: E402

TERMINAL = {"passed", "failed", "timeout", "error", "cancelled"}


class Harness:
    def __init__(self, token=None, vt=True, ports=("/dev/fake0",)):
        self.fws = {p: FakeFirmware(mac=f"7c:df:a1:a4:8f:{i:02x}", vt=vt)
                    for i, p in enumerate(ports)}
        self.opens = {p: 0 for p in ports}

        def opener(board):
            def open_():
                fw = self.fws[board.port]
                self.opens[board.port] += 1
                s = FakeSerial(fw, board.port)
                if self.opens[board.port] > 1:  # reopened after esptool: it reset the chip
                    fw.boot()
                return s
            return open_

        orig_board = esp32_plugin.Board

        class TestBoard(orig_board):
            def __init__(b, port, baud, events, open_serial=None):
                super().__init__(port, baud, events)
                b._open_serial = opener(b)

        esp32_plugin.Board = TestBoard
        self._orig_board = orig_board
        self._orig_candidates = Esp32Plugin._candidate_ports
        Esp32Plugin._candidate_ports = lambda s: list(ports)
        self.plugin = Esp32Plugin(ports=list(ports), rescan=False)
        self.server = PluginServer(self.plugin, "127.0.0.1", 0, token)
        self.plugin.jobs = self.server.jobs
        self.server.start_background()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.token = token
        # Wait for discovery (status probe) to finish.
        end = time.time() + 5
        while time.time() < end and any(b.vt is None for b in self.plugin.boards()):
            time.sleep(0.02)

    def close(self):
        self.server.shutdown()
        for b in self.plugin.boards():
            b.stop()
        esp32_plugin.Board = self._orig_board
        Esp32Plugin._candidate_ports = self._orig_candidates

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = dict(headers or {})
        if self.token:
            h.setdefault("X-Plugin-Token", self.token)
        if body is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def run(self, action, params=None, device=None, timeout_ms=None):
        body = {"action": action, "params": params or {}}
        if device:
            body["device"] = device
        if timeout_ms:
            body["timeout_ms"] = timeout_ms
        code, resp = self.call("POST", "/jobs", body)
        assert code == 202, (code, resp)
        jid = resp["job"]["id"]
        while True:
            code, job = self.call("GET", f"/jobs/{jid}?wait=10")
            assert code == 200
            if job["state"] in TERMINAL:
                return job


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_health(self):
        code, h = self.h.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(h["contract"], "visualtester-plugin")
        self.assertEqual(h["contract_version"], 1)
        self.assertEqual(h["plugin"]["name"], "esp32")
        self.assertEqual(h["devices"], 1)
        self.assertIn("events", h["capabilities"])

    def test_devices_use_mac_as_id(self):
        code, d = self.h.call("GET", "/devices")
        self.assertEqual(code, 200)
        dev = d["devices"][0]
        self.assertEqual(dev["id"], "7c:df:a1:a4:8f:00")
        self.assertEqual(dev["state"], "ready")
        self.assertEqual(dev["details"]["port"], "/dev/fake0")
        self.assertTrue(dev["details"]["vt_control"])

    def test_actions_have_schemas(self):
        code, a = self.h.call("GET", "/actions")
        names = {x["name"]: x for x in a["actions"]}
        self.assertEqual(sorted(names), ["broadcast-ssid", "clear", "expect", "factory-reset",
                                         "flash", "reset"])
        self.assertEqual(names["flash"]["params"]["firmware_dir"]["type"], "path")
        self.assertEqual(names["expect"]["params"]["script"],
                         {"type": "string", "required": True,
                          "description": names["expect"]["params"]["script"]["description"]})

    def test_unknown_action_and_device_and_params(self):
        code, r = self.h.call("POST", "/jobs", {"action": "nope"})
        self.assertEqual((code, r["code"]), (404, "unknown_action"))
        code, r = self.h.call("POST", "/jobs", {"action": "clear", "device": "zz"})
        self.assertEqual((code, r["code"]), (404, "unknown_device"))
        code, r = self.h.call("POST", "/jobs", {"action": "clear", "params": {"x": 1}})
        self.assertEqual((code, r["code"]), (400, "bad_request"))
        code, r = self.h.call("POST", "/jobs", {"action": "flash", "params": {"erase": "maybe"}})
        self.assertEqual((code, r["code"]), (400, "bad_request"))
        code, r = self.h.call("GET", "/jobs/j-missing")
        self.assertEqual((code, r["code"]), (404, "not_found"))

    def test_device_alias_by_port(self):
        job = self.h.run("clear", device="/dev/fake0")
        self.assertEqual(job["state"], "passed")
        self.assertEqual(job["device"], "7c:df:a1:a4:8f:00")

    def _never_ready(self):
        # A reset whose board never prints the ready line keeps the job running.
        self.h.fws["/dev/fake0"].ready_on_boot = False

    def test_busy_device_is_409(self):
        self._never_ready()
        code, r = self.h.call("POST", "/jobs", {"action": "reset", "timeout_ms": 2000})
        self.assertEqual(code, 202)
        code, r2 = self.h.call("POST", "/jobs", {"action": "clear"})
        self.assertEqual((code, r2["code"]), (409, "device_busy"))
        code, d = self.h.call("GET", "/devices")
        self.assertEqual(d["devices"][0]["state"], "busy")

    def test_timeout_state(self):
        self._never_ready()
        job = self.h.run("reset", timeout_ms=500)
        self.assertEqual(job["state"], "timeout")
        self.assertEqual(job["error"]["code"], "timeout")
        # the device is free again
        self.assertEqual(self.h.run("clear")["state"], "passed")

    def test_cancel(self):
        self._never_ready()
        code, r = self.h.call("POST", "/jobs", {"action": "reset", "timeout_ms": 20000})
        jid = r["job"]["id"]
        time.sleep(0.2)
        code, c = self.h.call("POST", f"/jobs/{jid}/cancel")
        self.assertEqual(code, 200)
        self.assertEqual(c["job"]["state"], "cancelled")

    def test_wait_returns_running_job_when_wait_expires(self):
        self._never_ready()
        code, r = self.h.call("POST", "/jobs", {"action": "reset", "timeout_ms": 5000})
        t = time.time()
        code, job = self.h.call("GET", f"/jobs/{r['job']['id']}?wait=0.3")
        self.assertEqual(job["state"], "running")
        self.assertLess(time.time() - t, 2)

    def test_events_stream(self):
        req = urllib.request.Request(self.h.base + "/events?since=0")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            got = []
            while len(got) < 2:
                line = r.readline().decode()
                if line.startswith("data: "):
                    got.append(json.loads(line[6:]))
        self.assertTrue(all("seq" in e and "type" in e for e in got))

    def test_upload_and_path_ref(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("build/flasher_args.json", "{}")
        code, r = self.h.call("POST", "/uploads", raw=buf.getvalue(),
                              headers={"Content-Type": "application/zip"})
        self.assertEqual(code, 201)
        self.assertTrue(r["upload"].startswith("upload:"))
        self.assertTrue(os.path.isfile(os.path.join(r["path"], "build", "flasher_args.json")))
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil", "x")
        code, r = self.h.call("POST", "/uploads", raw=bad.getvalue())
        self.assertEqual(code, 400)


class TokenTests(unittest.TestCase):
    def test_token_required(self):
        h = Harness(token="s3cret")
        try:
            code, r = h.call("GET", "/health", headers={"X-Plugin-Token": "wrong"})
            self.assertEqual((code, r["code"]), (401, "unauthorized"))
            code, r = h.call("GET", "/health")
            self.assertEqual(code, 200)
        finally:
            h.close()


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.fw = self.h.fws["/dev/fake0"]

    def tearDown(self):
        self.h.close()

    SCRIPT = """
        # the app sees a broken sensor
        GET /temperature.json -> 200 {"device":"{device}","temperature_c":0,"ok":false,"err":1}

        POST /setup -> 500
        wifi off
    """

    def test_expect_sends_each_rule_in_order(self):
        job = self.h.run("expect", {"script": self.SCRIPT})
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(job["result"], {"rules": 2, "applied": 3})
        sent = [l.split(" ", 2)[2] for l in self.fw.received if l.startswith("@vt p")]
        self.assertEqual(sent[-3:], [
            'rule GET /temperature.json -> 200 {"device":"{device}","temperature_c":0,'
            '"ok":false,"err":1}',
            "rule POST /setup -> 500",
            "rule wifi off"])
        self.assertNotIn("clear", sent)
        self.assertEqual(self.fw.wifi, "off")

    def test_blocks_only_state_what_changes(self):
        self.h.run("expect", {"script": "GET /a -> 500\nGET /b -> 500\nmqtt off"})
        # the next block changes /a only; /b and mqtt stay as they were
        job = self.h.run("expect", {"script": "GET /a -> hang"})
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(self.fw.rules, {("GET", "/a"): "hang", ("GET", "/b"): "500"})
        self.assertEqual(self.fw.mqtt, "off")
        # and `-> normal` undoes a single rule
        job = self.h.run("expect", {"script": "GET /b -> normal"})
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(job["result"]["rules"], 1)
        self.assertEqual(self.fw.rules, {("GET", "/a"): "hang"})

    def test_sequence_rule_is_relayed_unchanged(self):
        job = self.h.run("expect", {"script": "GET /temperature.json -> sequence 17 18 19"})
        self.assertEqual(job["state"], "passed", job)
        sent = [l.split(" ", 2)[2] for l in self.fw.received if l.startswith("@vt p")]
        self.assertEqual(sent[-1], "rule GET /temperature.json -> sequence 17 18 19")
        self.assertEqual(self.fw.rules, {("GET", "/temperature.json"): "sequence 17 18 19"})
        job = self.h.run("expect", {"script": "GET /temperature.json -> sequence 17 hot"})
        self.assertEqual(job["state"], "failed", job)

    def test_rejected_rule_stops_and_keeps_earlier_state(self):
        self.h.run("expect", {"script": "GET /old -> 500"})
        job = self.h.run("expect", {"script": "GET /a -> 500\nGET /b => 500\nGET /c -> 500"})
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "rule_rejected")
        self.assertEqual((job["result"]["line"], job["result"]["applied"]), (2, 1))
        self.assertIn("GET /b => 500", job["error"]["message"])
        self.assertEqual(set(self.fw.rules), {("GET", "/old"), ("GET", "/a")})
        self.assertNotIn("rule GET /c -> 500", " ".join(self.fw.received))

    def test_factory_reset(self):
        self.h.run("expect", {"script": "GET /a -> 500"})
        before = self.fw.reboots
        job = self.h.run("factory-reset")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(self.fw.reboots, before + 1)
        self.assertEqual(self.fw.provisioned_ssid, "")
        self.assertEqual(job["result"]["status"]["rules"], 0)
        self.assertEqual(job["result"]["status"]["ssid"], "MyAccessPoint")

    def test_factory_reset_needs_reboot(self):
        self.fw.ready_on_boot = False
        job = self.h.run("factory-reset", timeout_ms=60000)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "not_ready")

    def test_broadcast_ssid(self):
        job = self.h.run("broadcast-ssid")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(job["result"]["ssid"], "BJF-a48f00")
        self.assertIsNone(self.fw.ap_seconds)
        self.assertTrue(self.fw.received[-1].endswith(" ap-start"))
        job = self.h.run("broadcast-ssid", {"seconds": "0"})
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(self.fw.ap_seconds, 0)
        job = self.h.run("broadcast-ssid", {"seconds": 99999})
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "device_error")

    def test_empty_script_is_error(self):
        job = self.h.run("expect", {"script": "  \n# nothing\n"})
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"]["code"], "bad_request")

    def test_script_is_required(self):
        code, r = self.h.call("POST", "/jobs", {"action": "expect"})
        self.assertEqual((code, r["code"]), (400, "bad_request"))

    def test_clear(self):
        self.h.run("expect", {"script": "GET /a -> 500\nmqtt off"})
        job = self.h.run("clear")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(job["result"], {"rules": 0})
        self.assertEqual((self.fw.rules, self.fw.mqtt), ({}, "on"))

    def test_reset_waits_for_ready(self):
        before = self.fw.reboots
        job = self.h.run("reset")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(self.fw.reboots, before + 1)
        self.assertEqual(job["result"]["status"]["proto"], 2)

    def test_reset_fails_without_ready_line(self):
        self.fw.ready_on_boot = False
        job = self.h.run("reset", timeout_ms=60000)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "not_ready")

    def test_hung_firmware_is_failed_no_reply(self):
        self.fw.silent = True
        job = self.h.run("clear")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "no_reply")


class FlashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.build = os.path.join(self.tmp, "result", "build")
        os.makedirs(os.path.join(self.build, "bootloader"))
        os.makedirs(os.path.join(self.build, "partition_table"))
        for rel in ("bootloader/bootloader.bin", "partition_table/partition-table.bin",
                    "temp_sensor.bin"):
            with open(os.path.join(self.build, rel), "wb") as f:
                f.write(b"\0" * 64)
        with open(os.path.join(self.build, "flasher_args.json"), "w") as f:
            json.dump({
                "write_flash_args": ["--flash_mode", "dio", "--flash_size", "2MB",
                                     "--flash_freq", "80m"],
                "flash_files": {"0x0": "bootloader/bootloader.bin",
                                "0x8000": "partition_table/partition-table.bin",
                                "0x10000": "temp_sensor.bin"},
                "extra_esptool_args": {"after": "hard_reset", "before": "default_reset",
                                       "stub": True, "chip": "esp32c3"}}, f)
        self.log = os.path.join(self.tmp, "esptool.log")
        os.environ["ESPTOOL"] = f"{sys.executable} {os.path.join(HERE, 'fake_esptool.py')}"
        os.environ["FAKE_ESPTOOL_LOG"] = self.log
        os.environ.pop("FAKE_ESPTOOL_RC", None)
        self.h = Harness()

    def tearDown(self):
        self.h.close()
        for k in ("ESPTOOL", "FAKE_ESPTOOL_LOG", "FAKE_ESPTOOL_RC"):
            os.environ.pop(k, None)

    def test_flash_result_dir(self):
        job = self.h.run("flash", {"firmware_dir": os.path.join(self.tmp, "result")})
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(job["result"]["chip"], "esp32c3")
        with open(self.log) as f:
            argv = json.loads(f.readline())
        self.assertEqual(argv[argv.index("--chip") + 1], "esp32c3")
        self.assertEqual(argv[argv.index("--port") + 1], "/dev/fake0")
        i = argv.index("write_flash")
        self.assertEqual(argv[i + 1:i + 7], ["--flash_mode", "dio", "--flash_size", "2MB",
                                             "--flash_freq", "80m"])
        self.assertIn("0x10000", argv)
        self.assertEqual(self.h.plugin.boards()[0].model, "ESP32-C3")

    def test_flash_missing_dir_is_error(self):
        job = self.h.run("flash", {"firmware_dir": os.path.join(self.tmp, "nope")})
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"]["code"], "bad_firmware")

    def test_flash_tool_failure_is_error(self):
        os.environ["FAKE_ESPTOOL_RC"] = "2"
        job = self.h.run("flash", {"firmware_dir": self.build})
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"]["code"], "flash_failed")
        # port came back
        self.assertEqual(self.h.run("clear")["state"], "passed")

    def test_flash_non_vt_firmware_fails_ready_check(self):
        self.h.fws["/dev/fake0"].vt = False
        job = self.h.run("flash", {"firmware_dir": self.build}, timeout_ms=60000)
        self.assertEqual(job["state"], "failed")
        job = self.h.run("flash", {"firmware_dir": self.build, "wait_ready": False})
        self.assertEqual(job["state"], "passed")


class MultiBoardTests(unittest.TestCase):
    def test_device_required_with_two_boards(self):
        h = Harness(ports=("/dev/fake0", "/dev/fake1"))
        try:
            code, r = h.call("POST", "/jobs", {"action": "clear"})
            self.assertEqual((code, r["code"]), (400, "bad_request"))
            job = h.run("expect", {"script": "GET /x -> 500"}, device="7c:df:a1:a4:8f:01")
            self.assertEqual(job["state"], "passed")
            self.assertEqual(h.fws["/dev/fake1"].rules, {("GET", "/x"): "500"})
            self.assertEqual(h.fws["/dev/fake0"].rules, {})
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
