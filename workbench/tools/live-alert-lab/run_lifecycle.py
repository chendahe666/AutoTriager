"""Exercise the four incident stages with real requests and Prometheus."""

import argparse
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
LAB = Path(__file__).resolve().parent
METRIC = 'autotriager_checkout_latency_ms{service="checkout"}'
ALERT = "AutoTriagerCheckoutLatencyHigh"


def reserve_check(ports):
    with ExitStack() as stack:
        for port in ports:
            listener = stack.enter_context(socket.socket())
            listener.bind(("127.0.0.1", port))


class Client:
    def __init__(self, port, token=None):
        self.origin = f"http://127.0.0.1:{port}"
        self.headers = {"Origin": self.origin}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, body=None, *, expected=200):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {**self.headers, "Content-Type": "application/json"}
        try:
            with self.opener.open(Request(self.origin + path, payload, headers), timeout=20) as response:
                if response.status != expected:
                    raise RuntimeError(f"Unexpected response for {path}: {response.status}")
                raw = response.read()
                return json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw
        except HTTPError as exc:
            if exc.code == expected:
                return json.loads(exc.read())
            raise


def wait_for(description, probe, *, timeout=18):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (HTTPError, URLError, TimeoutError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {description}")


def demonstrate(lab, prom, workbench):
    wait_for("lab control", lambda: lab.request("/lab-control"))
    wait_for("Prometheus readiness", lambda: prom.request("/-/ready"))
    wait_for("workbench readiness", lambda: workbench.request("/api/v1/health"))
    workbench.request("/")
    workbench.headers["X-CSRF-Token"] = workbench.request("/api/v1/capabilities")["csrf_token"]
    wait_for("healthy baseline", lambda: lab.request("/evidence")["latency_ms"] < 100)
    episodes = []
    seen = set()
    for index in range(2):
        state = lab.request("/lab-control")
        inject = lab.request("/lab-control", {"operation": "set_payment_delay_ms", "expected_lab_id": state["lab_id"],
                             "expected_generation": state["generation"], "expected_delay_ms": 15, "delay_ms": 240})

        def ready_proposal():
            alerts = workbench.request("/api/v1/monitoring")["alerts"]
            candidates = [alert for alert in alerts if alert["incident_id"] not in seen]
            if not candidates:
                return None
            alert = candidates[0]
            path = f"/api/v1/incident-lifecycle/{alert['incident_id']}"
            detail = workbench.request(path)
            return (alert, path, detail) if detail["mitigation"] else None

        alert, path, before = wait_for("new alert with bound mitigation", ready_proposal)
        assert before["diagnosis"]["finding"]["status"] == "hypothesis"
        assert before["routing"][0]["decision"] == "new"
        assert before["prevention"] == []
        plan_id = before["mitigation"]["plan_id"]
        if index == 0:
            workbench.request(path + "/approve", {"plan_id": plan_id, "approve": False}, expected=400)
            assert lab.request("/lab-control")["generation"] == inject["generation"]
        result = workbench.request(path + "/approve", {"plan_id": plan_id, "approve": True})
        assert result["status"] == "verified"
        assert workbench.request(path + "/approve", {"plan_id": plan_id, "approve": True}) == result
        assert lab.request("/lab-control")["generation"] == inject["generation"] + 1
        after = workbench.request(path)
        assert after["diagnosis"]["finding"]["status"] == "confirmed_in_lab"
        seen.add(alert["incident_id"])
        assert bool(after["prevention"]) == (index == 1)
        episodes.append({"alert": alert, "injection": inject, "before": before, "after": after,
                         "case": workbench.request(f"/api/v1/triage/{alert['incident_id']}")["case"]})
        wait_for("Prometheus alarm clearing", lambda: not prom.request("/api/v1/alerts")["data"]["alerts"])
        print(f"Episode {index + 1} verified: {alert['incident_id']}", flush=True)
    return {"environment": "controlled local checkout and payment", "workbench": workbench.origin,
            "prometheus_version": prom.request("/api/v1/status/buildinfo")["data"]["version"],
            "checks": ["approval required", "approval retry does not reapply", "fresh recovery samples", "two verified episodes required for prevention"],
            "episodes": episodes}


def demonstrate_live_baro(lab, prom, workbench):
    wait_for("lab control", lambda: lab.request("/lab-control"))
    wait_for("Prometheus readiness", lambda: prom.request("/-/ready"))
    wait_for("workbench readiness", lambda: workbench.request("/api/v1/health"))
    workbench.request("/")
    wait_for("healthy baseline", lambda: lab.request("/evidence")["latency_ms"] < 100)
    print("Collecting a healthy reference window before introducing one fault.", flush=True)
    time.sleep(27)
    state = lab.request("/lab-control")
    injection = lab.request("/lab-control", {"operation": "set_payment_delay_ms", "expected_lab_id": state["lab_id"],
                            "expected_generation": state["generation"], "expected_delay_ms": 15, "delay_ms": 240})

    def ready():
        alerts = workbench.request("/api/v1/monitoring")["alerts"]
        if alerts:
            alert = alerts[0]
            case = workbench.request(f"/api/v1/triage/{alert['incident_id']}")["case"]
            if case["live_baro"]["status"] != "waiting":
                return alert, case

    alert, case = wait_for("the automatic live BARO result", ready, timeout=30)
    incident_id = alert["incident_id"]
    lifecycle = workbench.request(f"/api/v1/incident-lifecycle/{incident_id}")
    if lifecycle["action"] is not None or not lifecycle["mitigation"]:
        raise RuntimeError("This demonstration must start with operator approval pending")
    baro = case["live_baro"]
    evidence = [workbench.request(f"/api/v1/baro-evidence/{incident_id}/{item['evidence_id']}") for item in baro["ranking"]]
    print(f"Live BARO {baro['status']}: {workbench.origin}/#/errors/{incident_id}", flush=True)
    return {"environment": "controlled local checkout and payment", "workbench": workbench.origin,
            "case": case, "alert": alert, "injection": injection, "lifecycle_before_approval": lifecycle,
            "ranking_evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8846)
    parser.add_argument("--lab-port", type=int, default=9476)
    parser.add_argument("--payment-port", type=int, default=9477)
    parser.add_argument("--prometheus-port", type=int, default=9196)
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--live-baro-demo", action="store_true", help="Introduce one fault and leave recovery for the operator")
    args = parser.parse_args()
    if args.live_baro_demo and not args.keep_running:
        parser.error("Use --keep-running for the operator approval demonstration")
    lab_port, payment_port, prom_port = args.lab_port, args.payment_port, args.prometheus_port
    reserve_check((lab_port, payment_port, prom_port, args.port))
    if args.out.exists():
        raise FileExistsError(args.out)
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=False)
    token = secrets.token_hex(24)
    journal = state / "requests.jsonl"
    config = state / "prometheus.yml"
    config.write_text((LAB / "prometheus.yml").read_text().replace("127.0.0.1:9464", f"127.0.0.1:{lab_port}"))
    (state / "rules.yml").write_bytes((LAB / "rules.yml").read_bytes())
    prom_exe = ROOT / ".runtime/prometheus-3.10.0/prometheus-3.10.0.windows-amd64/prometheus.exe"
    subprocess.run([prom_exe.with_name("promtool.exe"), "check", "config", config], check=True)
    env = {**os.environ, "AUTOTRIAGER_LAB_CONTROL_TOKEN": token, "AUTOTRIAGER_LAB_CONTROL_PORT": str(lab_port),
           "AUTOTRIAGER_MONITORING_PROMETHEUS": f"http://127.0.0.1:{prom_port}", "AUTOTRIAGER_MONITORING_QUERY": METRIC,
           "AUTOTRIAGER_MONITORING_SERVICE": "checkout", "AUTOTRIAGER_MONITORING_METRIC": "Checkout request latency",
           "AUTOTRIAGER_MONITORING_UNIT": "ms", "AUTOTRIAGER_MONITORING_THRESHOLD": "100",
           "AUTOTRIAGER_MONITORING_BIN_SECONDS": "5", "AUTOTRIAGER_MONITORING_ALERT_RULE": ALERT,
           "AUTOTRIAGER_MONITORING_LAB_REQUEST_LOG": str(journal)}
    if args.live_baro_demo:
        env["AUTOTRIAGER_LIVE_BARO"] = "1"
    commands = {
        "checkout": [sys.executable, LAB / "checkout_lab.py", "--port", str(lab_port), "--payment-port", str(payment_port), "--evidence", journal],
        "prometheus": [prom_exe, f"--config.file={config}", f"--storage.tsdb.path={state / 'prometheus'}", f"--web.listen-address=127.0.0.1:{prom_port}", "--storage.tsdb.retention.time=1d"],
        "workbench": [sys.executable, ROOT / "scripts/launch.py", "--port", str(args.port), "--state-dir", state / "workbench", "--operations-profile"],
    }
    processes = []
    success = False
    try:
        for name, command in commands.items():
            with (state / f"{name}.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, env=env,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                processes.append(process)
        run = demonstrate_live_baro if args.live_baro_demo else demonstrate
        result = run(Client(lab_port, token), Client(prom_port), Client(args.port))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as output:
            json.dump(result, output, indent=2, allow_nan=False)
            output.write("\n")
        success = True
    finally:
        if success and args.keep_running:
            (state / "process-ids.json").write_text(json.dumps({name: proc.pid for name, proc in zip(commands, processes)}))
        else:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
