"""Local checkout and payment testbed with measured HTTP requests."""

import argparse
import ctypes
import hmac
import http.client
import json
import os
import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


METRIC = "autotriager_checkout_latency_ms"
SERVICE = "checkout"
BASE_MS = 15.0
FAULT_MS = 240.0
THRESHOLD_MS = 100.0
CADENCE = 0.5

LAB_METRICS = {
    "autotriager_checkout_probe_requests_total": ("counter", "Checkout probes attempted by this local testbed."),
    "autotriager_checkout_probe_errors_total": ("counter", "Checkout probes that returned HTTP 4xx or 5xx or failed to return a valid response."),
    "autotriager_checkout_lab_latency_ms": ("gauge", "Latest measured checkout latency in milliseconds for this testbed run."),
    "autotriager_checkout_lab_process_cpu_seconds_total": ("counter", "CPU time consumed by the local checkout and payment testbed process in seconds."),
    "autotriager_checkout_lab_process_resident_memory_bytes": ("gauge", "Resident memory of the local checkout and payment testbed process in bytes."),
    "autotriager_checkout_lab_host_cpu_utilization_percent": ("gauge", "CPU utilization across the host running the local testbed, in percent."),
    "autotriager_checkout_lab_host_memory_utilization_percent": ("gauge", "Physical memory used on the host running the local testbed, in percent."),
}


class WindowsResources:
    class ProcessMemory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                    ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                    ("peak_paged_pool", ctypes.c_size_t), ("paged_pool", ctypes.c_size_t),
                    ("peak_nonpaged_pool", ctypes.c_size_t), ("nonpaged_pool", ctypes.c_size_t),
                    ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]

    class HostMemory(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
                    ("total_pagefile", ctypes.c_uint64), ("available_pagefile", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
                    ("extended_virtual", ctypes.c_uint64)]

    def __init__(self):
        kernel = ctypes.WinDLL("kernel32")
        self.system_times = kernel.GetSystemTimes
        self.system_times.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
        self.system_times.restype = wintypes.BOOL
        self.memory_status = kernel.GlobalMemoryStatusEx
        self.memory_status.argtypes = [ctypes.POINTER(self.HostMemory)]
        self.memory_status.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.process = kernel.GetCurrentProcess()
        self.memory_info = kernel.K32GetProcessMemoryInfo
        self.memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(self.ProcessMemory), wintypes.DWORD]
        self.memory_info.restype = wintypes.BOOL

    def read(self):
        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        ticks = None
        if self.system_times(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            idle_ticks, kernel_ticks, user_ticks = [(part.dwHighDateTime << 32) | part.dwLowDateTime
                                                    for part in (idle, kernel, user)]
            ticks = (idle_ticks, kernel_ticks + user_ticks)

        host = self.HostMemory(length=ctypes.sizeof(self.HostMemory))
        host_memory = None
        if self.memory_status(ctypes.byref(host)) and host.total_physical:
            host_memory = (host.total_physical, host.available_physical)

        process = self.ProcessMemory(cb=ctypes.sizeof(self.ProcessMemory))
        rss = process.working_set if self.memory_info(self.process, ctypes.byref(process), process.cb) else None
        return ticks, host_memory, rss


def procfs_resources():
    ticks = host_memory = rss = None
    try:
        with Path("/proc/stat").open(encoding="ascii") as stream:
            totals = [int(part) for part in stream.readline().split()[1:9]]
        ticks = (totals[3] + totals[4], sum(totals))
    except (OSError, ValueError, IndexError):
        pass
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.split()[0]) * 1024
        host_memory = (memory["MemTotal"], memory["MemAvailable"])
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        rss = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    return ticks, host_memory, rss


class ResourceMetrics:
    def __init__(self):
        self.read = procfs_resources
        if os.name == "nt":
            try:
                self.read = WindowsResources().read
            except (AttributeError, OSError):
                pass
        self.previous_ticks = None
        self.lock = threading.Lock()

    def values(self):
        with self.lock:
            ticks, host_memory, rss = self.read()
            previous, self.previous_ticks = self.previous_ticks, ticks
        values = {"autotriager_checkout_lab_process_cpu_seconds_total": time.process_time()}
        if rss is not None and rss >= 0:
            values["autotriager_checkout_lab_process_resident_memory_bytes"] = rss
        if host_memory and 0 <= host_memory[1] <= host_memory[0] and host_memory[0] > 0:
            total, available = host_memory
            values["autotriager_checkout_lab_host_memory_utilization_percent"] = 100 * (total - available) / total
        if previous and ticks:
            idle = ticks[0] - previous[0]
            total = ticks[1] - previous[1]
            if total > 0 and 0 <= idle <= total:
                values["autotriager_checkout_lab_host_cpu_utilization_percent"] = 100 * (total - idle) / total
        return values


def get_local(port, path):
    with_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        with_connection.request("GET", path)
        response = with_connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        with_connection.close()


class Lab:
    def __init__(self, checkout_port, payment_port, evidence):
        self.checkout_port = checkout_port
        self.payment_port = payment_port
        self.evidence = evidence
        self.lock = threading.Lock()
        self.delay = lambda now: BASE_MS
        self.latest = None
        self.recent = deque(maxlen=12)
        self.lab_id = uuid.uuid4().hex
        self.generation = 0
        self.controlled_delay = None
        self.token = None
        self.probe_requests = 0
        self.probe_errors = 0
        self.resources = ResourceMetrics()

    def control_state(self):
        return {"lab_id": self.lab_id, "generation": self.generation,
                "delay_ms": self.controlled_delay, "baseline_delay_ms": int(BASE_MS)}

    def sample(self):
        start = time.perf_counter()
        try:
            status, record = get_local(self.checkout_port, "/checkout")
        except (OSError, ValueError, http.client.HTTPException):
            with self.lock:
                self.probe_requests += 1
                self.probe_errors += 1
            raise
        record["http_status"] = status
        record["observed_at_unix"] = time.time()
        record["latency_ms"] = (time.perf_counter() - start) * 1000
        if self.evidence:
            with self.evidence.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, separators=(",", ":")) + "\n")
        with self.lock:
            self.latest = record
            self.recent.append(record)
            self.probe_requests += 1
            self.probe_errors += int(status >= 400)
        return record

    def metrics(self):
        with self.lock:
            record, requests, errors = self.latest, self.probe_requests, self.probe_errors
        lines = [f"# HELP {METRIC} Latest measured HTTP checkout latency in milliseconds.", f"# TYPE {METRIC} gauge"]
        values = {**self.resources.values(), "autotriager_checkout_probe_requests_total": requests,
                  "autotriager_checkout_probe_errors_total": errors}
        if record and time.time() - record["observed_at_unix"] < 2:
            latency = record["latency_ms"]
            lines.append(f'{METRIC}{{service="{SERVICE}"}} {latency!r}')
            values["autotriager_checkout_lab_latency_ms"] = latency
        labels = f'{{service="{SERVICE}",lab_id="{self.lab_id}"}}'
        for name, value in values.items():
            kind, help_text = LAB_METRICS[name]
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", f"{name}{labels} {value!r}"))
        return "\n".join(lines) + "\n"


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def respond(self, status, body, kind="application/json"):
        payload = json.dumps(body).encode() if kind == "application/json" else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def payment_handler(lab):
    class Payment(LocalHandler):
        def do_GET(self):
            if self.path != "/authorize":
                self.respond(404, {"error": "not found"})
                return
            with lab.lock:
                state = lab.control_state()
                delay = lab.delay(time.monotonic()) if state["delay_ms"] is None else state["delay_ms"]
            time.sleep(delay / 1000)
            self.respond(200, {"authorization": "approved", "control": state if lab.token else None})

    return Payment


def checkout_handler(lab):
    class Checkout(LocalHandler):
        def control_authorized(self):
            return lab.token is not None and hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), b"Bearer " + lab.token.encode())

        def do_POST(self):
            if self.path != "/lab-control" or not self.control_authorized():
                self.respond(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError()
                operation = json.loads(self.rfile.read(length))
                if (set(operation) != {"operation", "expected_lab_id", "expected_generation", "expected_delay_ms", "delay_ms"}
                        or operation["operation"] != "set_payment_delay_ms"
                        or any(type(operation[k]) is not int for k in ("expected_generation", "expected_delay_ms", "delay_ms"))
                        or {operation["expected_delay_ms"], operation["delay_ms"]} != {int(BASE_MS), int(FAULT_MS)}):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "unsupported control operation"})
                return
            with lab.lock:
                state = lab.control_state()
                matched = all(operation[f"expected_{key}"] == state[key] for key in ("lab_id", "generation", "delay_ms"))
                if matched:
                    lab.controlled_delay = operation["delay_ms"]
                    lab.generation += 1
                    state = {**lab.control_state(), "status": "applied", "applied_at_unix": time.time()}
            self.respond(200 if matched else 409, state if matched else {"error": "lab state changed"})

        def do_GET(self):
            if self.path == "/lab-recovery" and self.control_authorized():
                with lab.lock:
                    samples = list(lab.recent)
                self.respond(200, {"samples": samples})
                return
            if self.path == "/lab-control" and self.control_authorized():
                with lab.lock:
                    state = lab.control_state()
                self.respond(200, state)
                return
            if self.path == "/metrics":
                self.respond(200, lab.metrics(), "text/plain; version=0.0.4; charset=utf-8")
                return
            if self.path == "/evidence":
                with lab.lock:
                    record = lab.latest
                self.respond(200 if record else 404, record or {"error": "no completed probe"})
                return
            if self.path != "/checkout":
                self.respond(404, {"error": "not found"})
                return
            start = time.perf_counter()
            status, payment = get_local(lab.payment_port, "/authorize")
            self.respond(status, {
                "request_id": uuid.uuid4().hex,
                "service": SERVICE,
                "route": "/checkout",
                "dependency": "payment",
                "dependency_route": "/authorize",
                "dependency_status": status,
                "dependency_latency_ms": (time.perf_counter() - start) * 1000,
                **({"control": payment["control"]} if payment.get("control") else {}),
            })

    return Checkout


@contextmanager
def serve(port, handler):
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9464)
    parser.add_argument("--payment-port", type=int, default=9465)
    parser.add_argument("--demo-fault-after", type=float, metavar="SECONDS")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
    lab = Lab(args.port, args.payment_port, args.evidence)
    token = os.environ.get("AUTOTRIAGER_LAB_CONTROL_TOKEN")
    if token:
        if not (len(token) >= 32 and token.isascii() and token.isalnum()) or args.demo_fault_after is not None or not args.evidence:
            parser.error("Controlled mode requires an alphanumeric token and evidence path, and uses explicit fault injection")
        lab.token = token
        lab.controlled_delay = int(BASE_MS)
    with ExitStack() as stack:
        stack.enter_context(serve(args.payment_port, payment_handler(lab)))
        stack.enter_context(serve(args.port, checkout_handler(lab)))
        print(f"Checkout lab listening on 127.0.0.1:{args.port}", flush=True)
        if args.demo_fault_after is not None:
            onset = time.monotonic() + args.demo_fault_after
            lab.delay = lambda now: FAULT_MS if now >= onset else BASE_MS
            print(f"Payment delay becomes {FAULT_MS:g} ms after {args.demo_fault_after:g} s", flush=True)
        try:
            while True:
                start = time.monotonic()
                try:
                    lab.sample()
                except (OSError, ValueError, http.client.HTTPException) as error:
                    print(f"Checkout probe failed: {error}", flush=True)
                time.sleep(max(0, CADENCE - (time.monotonic() - start)))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
