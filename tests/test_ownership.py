"""Board roles and cross-plugin ownership for the ESP32-SENSOR plugin.

The hub/server boards and the fake server belong to the ESP32-SERVER plugin.
This plugin probes every node, drops a board that answers `status` with the
other role and releases its console lock so the other plugin can adopt it;
the shared role cache keeps a later scan from touching that node again.

Run: python3 -m unittest discover -s tests   (needs pyserial)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

os.environ.setdefault("ESP32_READY_TIMEOUT_S", "3")
import esp32_plugin  # noqa: E402
from esp32_plugin import Esp32Plugin  # noqa: E402
from test_plugin import Harness  # noqa: E402

SENSOR = "7c:df:a1:a4:8f:00"   # /dev/fake0
HUB = "7c:df:a1:a4:8f:01"      # /dev/fake1
TWO = ("/dev/fake0", "/dev/fake1")


def cache_read_role(path: str, key: str):
    try:
        with open(path) as f:
            return json.load(f).get(key)
    except (OSError, json.JSONDecodeError):
        return None


class RoleTests(unittest.TestCase):
    def harness(self, **kw):
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h

    def test_defaults_to_the_sensor_and_hub_refs_are_rejected(self):
        h = self.harness(ports=TWO, roles=("sensor", "hub"))
        self.assertEqual(h.run("clear")["device"], SENSOR)
        self.assertEqual(h.run("clear", device="sensor")["device"], SENSOR)
        code, r = h.call("POST", "/jobs", {"action": "clear", "device": "hub"})
        self.assertEqual((code, r["code"]), (404, "wrong_plugin"))
        code, r = h.call("POST", "/jobs", {"action": "clear", "device": "server"})
        self.assertEqual((code, r["code"]), (404, "wrong_plugin"))
        code, d = h.call("GET", "/devices")
        self.assertEqual({x["id"]: x["details"]["role"] for x in d["devices"]},
                         {SENSOR: "sensor"})

    def test_firmware_without_role_is_a_sensor(self):
        h = self.harness(roles=(None,))
        code, d = h.call("GET", "/devices")
        self.assertEqual(d["devices"][0]["details"]["role"], "sensor")
        self.assertEqual(h.run("clear", device="sensor")["state"], "passed")

    def test_only_hub_boards_attached_is_no_sensor(self):
        h = self.harness(ports=TWO, roles=("hub", "hub"))
        code, r = h.call("POST", "/jobs", {"action": "clear", "device": "sensor"})
        self.assertEqual((code, r["code"]), (404, "unknown_device"))
        code, d = h.call("GET", "/devices")
        self.assertEqual(d["devices"], [])


class OwnershipTests(unittest.TestCase):
    """The two plugins share the boards through a per-node flock and a role
    cache. Here the flock primitives are simulated (one process stands in for
    two): a node a plugin took stays busy until it releases it."""

    def test_hub_board_is_probed_dropped_and_cached(self):
        holder = {}

        def try_lock(lock_dir, port):
            if holder.get(port):
                return None  # the other plugin holds this console
            holder[port] = True
            return 1

        def drop(self, port):
            holder.pop(port, None)

        orig_try = esp32_plugin.try_lock_port
        orig_drop = Esp32Plugin._drop_lock
        esp32_plugin.try_lock_port = try_lock
        Esp32Plugin._drop_lock = drop
        cache = os.path.join(tempfile.mkdtemp(), "roles.json")
        try:
            h = Harness(ports=TWO, roles=("sensor", "hub"),
                        lock_dir="sim", role_cache=cache)
            try:
                time.sleep(0.2)  # let the identify threads finish the handover
                code, d = h.call("GET", "/devices")
                self.assertEqual({x["id"]: x["details"]["role"] for x in d["devices"]},
                                 {SENSOR: "sensor"})
                # It took the hub board's node, probed it, dropped it: the node
                # is free for ESP32-SERVER and its role is in the cache under
                # both the node path and the MAC.
                self.assertNotIn("/dev/fake1", holder)
                self.assertEqual(cache_read_role(cache, "/dev/fake1"), "hub")
                self.assertEqual(cache_read_role(cache, SENSOR), "sensor")
                self.assertEqual(cache_read_role(cache, HUB), "hub")
                # the sensor's own node stays locked for this plugin
                self.assertIn("/dev/fake0", holder)
            finally:
                h.close()
        finally:
            esp32_plugin.try_lock_port = orig_try
            Esp32Plugin._drop_lock = orig_drop


class RealFlockTests(unittest.TestCase):
    """The console lock against a second, real process (not simulated): the
    sibling plugin must not get a node this process holds, and gets it once
    released. Guards against a lock call that silently does nothing."""

    def other_process_try(self, lock_dir, port):
        code = ("import sys; sys.path.insert(0, %r); import esp32_plugin as p; "
                "r = p.try_lock_port(%r, %r); print('none' if r is None else 'got')"
                % (os.path.dirname(HERE), lock_dir, port))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_a_held_node_is_refused_to_another_process(self):
        lock_dir = tempfile.mkdtemp()
        fd = esp32_plugin.try_lock_port(lock_dir, "/dev/null")
        self.assertNotIn(fd, (None, False))
        try:
            self.assertEqual(self.other_process_try(lock_dir, "/dev/null"), "none")
        finally:
            os.close(fd)
        self.assertEqual(self.other_process_try(lock_dir, "/dev/null"), "got")


if __name__ == "__main__":
    unittest.main()
