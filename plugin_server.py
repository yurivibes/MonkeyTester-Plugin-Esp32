"""Generic VisualTester Device Plugin API server (contract_version 1).

Implements the HTTP side of PLUGIN_API.md (in the VisualTester repo): health,
devices, actions, jobs (start / long-poll wait / cancel), server-sent events and
zip uploads. It knows nothing about ESP32 boards: a plugin hands it a `Plugin`
object (see esp32_plugin.py) and gets the whole contract for free.

Standard library only.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import socket
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONTRACT = "visualtester-plugin"
CONTRACT_VERSION = 1

TERMINAL_STATES = {"passed", "failed", "timeout", "error", "cancelled"}
JOB_RETENTION_S = 15 * 60
JOB_LOG_LINES = 500
MAX_WAIT_S = 60
UPLOAD_TTL_S = 60 * 60
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def now_ms() -> int:
    return int(time.time() * 1000)


# ───────────────────────────── action outcomes ─────────────────────────────


class ActionFailed(Exception):
    """The action ran but the device's response was wrong -> state `failed`."""

    def __init__(self, message: str, code: str = "unexpected_response", result=None):
        super().__init__(message)
        self.code = code
        self.result = result


class ActionError(Exception):
    """The plugin couldn't run the action -> state `error`."""

    def __init__(self, message: str, code: str = "plugin_error", result=None):
        super().__init__(message)
        self.code = code
        self.result = result


class JobTimeout(Exception):
    """Raised inside an action when its deadline passes -> state `timeout`."""


class JobCancelled(Exception):
    """Raised inside an action after POST /jobs/<id>/cancel -> state `cancelled`."""


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


# ───────────────────────────── action schema ─────────────────────────────


@dataclass
class Param:
    type: str  # string | int | float | bool | enum | path | json
    description: str = ""
    required: bool = False
    default: object = None
    values: list | None = None  # for enum

    def to_json(self):
        out = {"type": self.type, "required": self.required}
        if self.description:
            out["description"] = self.description
        if self.default is not None:
            out["default"] = self.default
        if self.values is not None:
            out["values"] = list(self.values)
        return out


@dataclass
class Action:
    name: str
    description: str
    run: object  # callable(ctx: JobContext, params: dict) -> dict | None
    params: dict = field(default_factory=dict)  # name -> Param
    default_timeout_ms: int = 30_000
    needs_device: bool = True

    def to_json(self):
        return {
            "name": self.name,
            "description": self.description,
            "params": {k: p.to_json() for k, p in self.params.items()},
            "default_timeout_ms": self.default_timeout_ms,
            "needs_device": self.needs_device,
        }


def _coerce(name: str, p: Param, value, resolve_path):
    """Validate/coerce one param value. Strings are accepted for every type so
    test steps (`key=value`) work without the client knowing the schema."""
    t = p.type
    try:
        if t == "string":
            if not isinstance(value, str):
                raise ValueError("expected a string")
            return value
        if t == "int":
            if isinstance(value, bool):
                raise ValueError("expected an integer")
            if isinstance(value, str):
                value = int(value.strip(), 0)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise ValueError("expected an integer")
            return value
        if t == "float":
            if isinstance(value, bool):
                raise ValueError("expected a number")
            if isinstance(value, str):
                value = float(value.strip())
            if not isinstance(value, (int, float)):
                raise ValueError("expected a number")
            return float(value)
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected a boolean")
        if t == "enum":
            if value not in (p.values or []):
                raise ValueError(f"expected one of {p.values}")
            return value
        if t == "path":
            if not isinstance(value, str) or not value:
                raise ValueError("expected a path")
            return resolve_path(value)
        if t == "json":
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
            return value
    except HttpError:
        raise
    except (ValueError, TypeError) as e:
        raise HttpError(400, "bad_request", f"param {name!r}: {e}") from None
    raise HttpError(500, "internal", f"param {name!r} has unknown type {t!r}")


def validate_params(action: Action, params: dict, resolve_path) -> dict:
    if not isinstance(params, dict):
        raise HttpError(400, "bad_request", "params must be an object")
    unknown = sorted(set(params) - set(action.params))
    if unknown:
        raise HttpError(400, "bad_request",
                        f"unknown param(s) for {action.name}: {', '.join(unknown)}")
    out = {}
    for name, p in action.params.items():
        if name in params and params[name] is not None:
            out[name] = _coerce(name, p, params[name], resolve_path)
        elif p.required:
            raise HttpError(400, "bad_request", f"missing required param {name!r}")
        else:
            out[name] = p.default
    return out


# ───────────────────────────── events ─────────────────────────────


class EventBus:
    """Ring buffer of events with a monotonically increasing seq."""

    def __init__(self, size: int = 2000):
        self._buf: deque = deque(maxlen=size)
        self._seq = 0
        self._cond = threading.Condition()

    def publish(self, device: str | None, type_: str, data) -> None:
        with self._cond:
            self._seq += 1
            self._buf.append({"seq": self._seq, "ts_ms": now_ms(),
                              "device": device, "type": type_, "data": data})
            self._cond.notify_all()

    def since(self, seq: int, device: str | None = None, timeout: float = 0.0) -> list:
        with self._cond:
            def pick():
                return [e for e in self._buf if e["seq"] > seq
                        and (device is None or e["device"] == device)]
            items = pick()
            if not items and timeout > 0:
                self._cond.wait(timeout)
                items = pick()
            return items

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq


# ───────────────────────────── jobs ─────────────────────────────


class JobContext:
    """Handed to an action. Gives it logging, the deadline and cancellation."""

    def __init__(self, job: "Job", manager: "JobManager"):
        self._job = job
        self._manager = manager
        self.device = job.device_obj
        self.deadline = time.monotonic() + job.timeout_ms / 1000.0
        self.cancel_event = threading.Event()
        self.started_monotonic = time.monotonic()

    @property
    def job_id(self) -> str:
        return self._job.id

    def log(self, line: str) -> None:
        self._manager.append_log(self._job, line)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check(self) -> None:
        """Raise if the job was cancelled or ran out of time."""
        if self.cancel_event.is_set():
            raise JobCancelled()
        if self.remaining() <= 0:
            raise JobTimeout()

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check()
            left = min(end - time.monotonic(), self.remaining())
            if left <= 0:
                self.check()
                return
            self.cancel_event.wait(min(left, 0.2))


@dataclass
class Job:
    id: str
    device: str | None
    device_obj: object
    action: str
    params: dict
    timeout_ms: int
    state: str = "queued"
    started_ms: int | None = None
    finished_ms: int | None = None
    result: object = None
    error: dict | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=JOB_LOG_LINES))
    ctx: JobContext | None = None
    created_monotonic: float = field(default_factory=time.monotonic)

    def to_json(self):
        return {
            "id": self.id,
            "device": self.device,
            "action": self.action,
            "params": self.params,
            "state": self.state,
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
            "result": self.result,
            "error": self.error,
            "log": list(self.log),
        }


class JobManager:
    def __init__(self, plugin, events: EventBus):
        self.plugin = plugin
        self.events = events
        self._jobs: dict[str, Job] = {}
        self._busy: dict[str, str] = {}  # device id -> job id
        self._cond = threading.Condition()

    # -- bookkeeping ----------------------------------------------------
    def append_log(self, job: Job, line: str) -> None:
        with self._cond:
            job.log.append(line)

    def _set_state(self, job: Job, state: str, **changes) -> None:
        with self._cond:
            if job.state in TERMINAL_STATES:
                return
            job.state = state
            for k, v in changes.items():
                setattr(job, k, v)
            if state in TERMINAL_STATES:
                job.finished_ms = now_ms()
                if job.device and self._busy.get(job.device) == job.id:
                    del self._busy[job.device]
            self._cond.notify_all()
            snapshot = job.to_json()
        self.events.publish(job.device, "job", snapshot)

    def _prune(self) -> None:
        cutoff = time.monotonic() - JOB_RETENTION_S
        for jid in [j.id for j in self._jobs.values()
                    if j.state in TERMINAL_STATES and j.created_monotonic < cutoff]:
            del self._jobs[jid]

    def is_busy(self, device_id: str) -> bool:
        with self._cond:
            return device_id in self._busy

    # -- API ------------------------------------------------------------
    def start(self, body: dict) -> Job:
        if not isinstance(body, dict):
            raise HttpError(400, "bad_request", "body must be a JSON object")
        name = body.get("action")
        if not isinstance(name, str):
            raise HttpError(400, "bad_request", "missing 'action'")
        action = self.plugin.actions().get(name)
        if action is None:
            raise HttpError(404, "unknown_action", f"unknown action {name!r}")
        params = validate_params(action, body.get("params") or {}, self.plugin.resolve_path)

        timeout_ms = body.get("timeout_ms")
        if timeout_ms is None:
            timeout_ms = action.default_timeout_ms
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise HttpError(400, "bad_request", "timeout_ms must be a positive integer")

        device_id, device_obj = None, None
        if action.needs_device:
            device_id, device_obj = self.plugin.resolve_device(body.get("device"))

        job = Job(id="j-" + uuid.uuid4().hex[:12], device=device_id, device_obj=device_obj,
                  action=name, params=params, timeout_ms=timeout_ms)
        with self._cond:
            self._prune()
            if device_id is not None:
                if device_id in self._busy:
                    raise HttpError(409, "device_busy",
                                    f"device {device_id} is running job {self._busy[device_id]}")
                self._busy[device_id] = job.id
            self._jobs[job.id] = job
        job.ctx = JobContext(job, self)
        threading.Thread(target=self._run, args=(job, action), daemon=True,
                         name=f"job-{job.id}").start()
        threading.Thread(target=self._watchdog, args=(job,), daemon=True).start()
        return job

    def _watchdog(self, job: Job) -> None:
        # Ends the job on time even if the action is stuck in a blocking call.
        while job.state not in TERMINAL_STATES:
            left = job.ctx.remaining()
            if left <= 0:
                job.ctx.cancel_event.set()
                self.append_log(job, f"timeout after {job.timeout_ms} ms")
                self._set_state(job, "timeout", error={
                    "code": "timeout", "message": f"no response within {job.timeout_ms} ms"})
                return
            with self._cond:
                self._cond.wait(min(left, 1.0))

    def _run(self, job: Job, action: Action) -> None:
        self._set_state(job, "running", started_ms=now_ms())
        try:
            result = action.run(job.ctx, dict(job.params))
            self._set_state(job, "passed", result=result if result is not None else {})
        except ActionFailed as e:
            self.append_log(job, f"FAILED: {e}")
            self._set_state(job, "failed", result=e.result,
                            error={"code": e.code, "message": str(e)})
        except ActionError as e:
            self.append_log(job, f"ERROR: {e}")
            self._set_state(job, "error", result=e.result,
                            error={"code": e.code, "message": str(e)})
        except JobTimeout:
            self._set_state(job, "timeout", error={
                "code": "timeout", "message": f"no response within {job.timeout_ms} ms"})
        except JobCancelled:
            self._set_state(job, "cancelled", error={"code": "cancelled", "message": "cancelled"})
        except Exception as e:  # noqa: BLE001 - plugin bug, report not crash
            self.append_log(job, traceback.format_exc().rstrip())
            self._set_state(job, "error", error={"code": "internal", "message": repr(e)})

    def get(self, job_id: str, wait_s: float = 0.0) -> Job:
        end = time.monotonic() + max(0.0, min(wait_s, MAX_WAIT_S))
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                raise HttpError(404, "not_found", f"unknown job {job_id!r}")
            while job.state not in TERMINAL_STATES:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return job

    def cancel(self, job_id: str) -> Job:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                raise HttpError(404, "not_found", f"unknown job {job_id!r}")
        if job.state not in TERMINAL_STATES:
            job.ctx.cancel_event.set()
            self.append_log(job, "cancel requested")
            if job.state == "queued":
                self._set_state(job, "cancelled",
                                error={"code": "cancelled", "message": "cancelled"})
            # A running action notices the event at its next ctx.check(); give it a
            # moment, then end the job regardless so callers aren't left waiting.
            self.get(job_id, 2.0)
            self._set_state(job, "cancelled", error={"code": "cancelled", "message": "cancelled"})
        return job


# ───────────────────────────── uploads ─────────────────────────────


class Uploads:
    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(tempfile.gettempdir(), "vt-plugin-uploads")
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def _prune(self) -> None:
        cutoff = time.time() - UPLOAD_TTL_S
        for name in os.listdir(self.root):
            p = os.path.join(self.root, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    def store_zip(self, data: bytes) -> tuple[str, str]:
        with self._lock:
            self._prune()
        uid = uuid.uuid4().hex[:12]
        dest = os.path.join(self.root, uid)
        os.makedirs(dest)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                base = os.path.realpath(dest)
                for info in zf.infolist():
                    target = os.path.realpath(os.path.join(dest, info.filename))
                    if not (target == base or target.startswith(base + os.sep)):
                        raise HttpError(400, "bad_request",
                                        f"zip entry escapes the upload dir: {info.filename}")
                zf.extractall(dest)
        except zipfile.BadZipFile:
            shutil.rmtree(dest, ignore_errors=True)
            raise HttpError(400, "bad_request", "body is not a zip archive") from None
        except HttpError:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return "upload:" + uid, dest

    def resolve(self, ref: str) -> str:
        uid = ref[len("upload:"):]
        if not uid or not all(c in "0123456789abcdef" for c in uid):
            raise HttpError(400, "bad_request", f"bad upload reference {ref!r}")
        p = os.path.join(self.root, uid)
        if not os.path.isdir(p):
            raise HttpError(400, "bad_request", f"upload {ref!r} doesn't exist (expired?)")
        return p


# ───────────────────────────── plugin interface ─────────────────────────────


class Plugin:
    """What a concrete plugin implements. See esp32_plugin.Esp32Plugin."""

    name = "plugin"
    version = "0.0.0"
    capabilities: list = []

    def __init__(self):
        self.events = EventBus()
        self.uploads = Uploads()

    def actions(self) -> dict:  # name -> Action
        raise NotImplementedError

    def devices(self) -> list:  # list of device JSON dicts
        raise NotImplementedError

    def device_error(self):
        return None

    def resolve_device(self, ref) -> tuple:  # -> (device_id, device_obj)
        raise NotImplementedError

    def resolve_path(self, value: str) -> str:
        if value.startswith("upload:"):
            return self.uploads.resolve(value)
        return os.path.abspath(os.path.expanduser(value))


# ───────────────────────────── HTTP ─────────────────────────────


def make_handler(plugin: Plugin, jobs: JobManager, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"vt-plugin-{plugin.name}/{plugin.version}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter default logging
            if os.environ.get("PLUGIN_ACCESS_LOG"):
                super().log_message(fmt, *args)

        # -- helpers --
        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, e: HttpError) -> None:
            self._send(e.status, {"error": str(e), "code": e.code})

        def _auth(self) -> None:
            if token and self.headers.get("X-Plugin-Token") != token:
                raise HttpError(401, "unauthorized", "missing or wrong X-Plugin-Token")

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD_BYTES:
                raise HttpError(400, "bad_request", "body too large")
            return self.rfile.read(n) if n else b""

        def _json_body(self):
            raw = self._body()
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError as e:
                raise HttpError(400, "bad_request", f"invalid JSON: {e}") from None

        def _dispatch(self, method: str) -> None:
            try:
                self._auth()
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                self._route(method, parts, query)
            except HttpError as e:
                self._error(e)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                try:
                    self._error(HttpError(500, "internal", repr(e)))
                except OSError:
                    pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # -- routes --
        def _route(self, method, parts, query):
            if method == "GET" and parts == ["health"]:
                devs = plugin.devices()
                return self._send(200, {
                    "ok": plugin.device_error() is None,
                    "contract": CONTRACT,
                    "contract_version": CONTRACT_VERSION,
                    "plugin": {"name": plugin.name, "version": plugin.version},
                    "host": {"os": platform.system().lower(), "hostname": socket.gethostname()},
                    "devices": len(devs),
                    "capabilities": list(plugin.capabilities),
                    "error": plugin.device_error(),
                })
            if method == "GET" and parts == ["devices"]:
                return self._send(200, {"devices": plugin.devices(),
                                        "error": plugin.device_error()})
            if method == "GET" and parts == ["actions"]:
                return self._send(200, {"actions": [a.to_json()
                                                    for a in plugin.actions().values()]})
            if method == "POST" and parts == ["jobs"]:
                body = self._json_body()
                job = jobs.start(body)
                return self._send(202, {"job": job.to_json()})
            if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
                try:
                    wait = float(query.get("wait", "0"))
                except ValueError:
                    raise HttpError(400, "bad_request", "wait must be a number") from None
                if not 0 <= wait <= MAX_WAIT_S:
                    raise HttpError(400, "bad_request", f"wait must be 0-{MAX_WAIT_S}")
                job = jobs.get(parts[1], wait)
                return self._send(200, job.to_json())
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return self._send(200, {"job": jobs.cancel(parts[1]).to_json()})
            if method == "GET" and parts == ["events"] and "events" in plugin.capabilities:
                return self._events(query)
            if method == "POST" and parts == ["uploads"] and "uploads" in plugin.capabilities:
                ref, path = plugin.uploads.store_zip(self._body())
                return self._send(201, {"upload": ref, "path": path})
            raise HttpError(404, "not_found", f"no route for {method} {self.path}")

        def _events(self, query):
            device = query.get("device")
            try:
                seq = int(query.get("since", plugin.events.last_seq))
            except ValueError:
                raise HttpError(400, "bad_request", "since must be an integer") from None
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last_ping = time.monotonic()
            while True:
                for e in plugin.events.since(seq, device, timeout=1.0):
                    seq = e["seq"]
                    self.wfile.write(f"id: {seq}\ndata: {json.dumps(e)}\n\n".encode())
                if time.monotonic() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    last_ping = time.monotonic()
                self.wfile.flush()

    return Handler


class PluginServer:
    def __init__(self, plugin: Plugin, host: str = "0.0.0.0", port: int = 0,
                 token: str | None = None):
        self.plugin = plugin
        self.jobs = JobManager(plugin, plugin.events)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(plugin, self.jobs, token))
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
