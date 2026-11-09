# AutoTriager Workbench

Dahe Chen · Incident investigation and recovery in a local checkout lab.

AutoTriager places Prometheus monitoring, an alert queue, Metric BARO rankings, and request-level evidence in one workspace. A reproducible lab sends HTTP checkout requests to a payment dependency and introduces an operator-observable delay. You can inspect the resulting alert and approve a narrowly scoped reset, then check measured recovery.

## Windows setup

Install Git, Python 3.11 or newer, and Node.js 22.12.0 or newer. Python 3.13 is the tested runtime. Commands below run in PowerShell from this directory, which contains `apps`, `backend`, and `tools`.

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Push-Location .\apps\web
npm.cmd ci
npm.cmd run build
Pop-Location

git clone --config core.autocrlf=false https://github.com/phamquiluan/RCAEval.git .vendor/rcaeval
git -C .vendor/rcaeval checkout --detach dc9f48786db50cf9469f2c596b79f01907d4ac91
```

The workbench loads named functions from the pinned RCAEval revision after checking the SHA-256 of the source files. Download the [Prometheus 3.10.0 Windows AMD64 archive](https://github.com/prometheus/prometheus/releases/tag/v3.10.0). Extract it so both executables are under `.runtime/prometheus-3.10.0/prometheus-3.10.0.windows-amd64/`. The launcher checks the configuration with `promtool.exe` and starts `prometheus.exe` from that directory.

## Run the live demo

```powershell
& .\.venv\Scripts\python.exe .\tools\live-alert-lab\run_lifecycle.py --live-baro-demo --keep-running --state .local-live-demo --out .local-live-demo/before-approval.json
```

The runner prints the alert's investigation URL after collecting a healthy reference and recording one fault. Open that URL or [the local incident workspace](http://127.0.0.1:8846/#/errors). The default ports are 8846 (workbench), 9196 (Prometheus), 9476 (checkout), and 9477 (payment), all on loopback. Use a new `--state` directory and `--out` path per run, and override the ports with `--help` if needed.

1. In **Monitoring**, inspect the observed checkout request latency, the 100 ms threshold, and the experimental forecast. In **Warnings**, open the received alert to see its investigation in the same workspace.
2. Inspect the Metric BARO ranking and its source rows. Prometheus scrapes the checkout latency for the alert, and the probe journal retains both checkout and payment timings for ranking. The alert timestamp is the incident split and the ranking uses a bounded window before operator approval.
3. Inspect the proposed recovery. After checking the consent box, selecting **Approve lab recovery** applies the displayed 240 ms to 15 ms payment delay reset in this lab. The workbench verifies three consecutive new checkout requests from the updated lab state, with HTTP 200 and latency at or below 100 ms.

To stop the same still-running demo, use its recorded process IDs:

```powershell
$demoProcesses = Get-Content .\.local-live-demo\process-ids.json -Raw | ConvertFrom-Json
Stop-Process -Id $demoProcesses.checkout,$demoProcesses.prometheus,$demoProcesses.workbench
```

The live demonstration runs without a model key, published datasets, or a production connection. Recorded archives, raw investigation receipts, provider qualification state, and original datasets are separate local inputs. Historical replay and optional provider views depend on those inputs. The supplied aggregate [RE2 Online Boutique comparison](verification/re2ob-multisource-baro-20261108/receipt.json) covers 90 development cases and is separate from the live checkout demonstration.

## Checks

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m pytest -q tests
Push-Location .\apps\web
npm.cmd test
Pop-Location
```

See [third-party notices](THIRD_PARTY_NOTICES.md) for dependency sources and licensing.
