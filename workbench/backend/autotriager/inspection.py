"""Bounded actual-data inspection, with no language model or diagnosis."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading

from .gateway import GatewayDenied


class InspectionWorker:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autotriager-inspection")
        self._lock = threading.Lock()
        self._submitted = set()
        self._closed = False

    def submit(self, run_id):
        with self._lock:
            if self._closed:
                raise RuntimeError("Inspection worker is closed")
            if run_id in self._submitted:
                return None
            self._submitted.add(run_id)
            return self._executor.submit(self._execute, run_id)

    def _execute(self, run_id):
        token = None
        try:
            if not self.store.start_inspection(run_id):
                return
            token = self.gateway.issue_capability(run_id)
            observations, unknowns = [], [
                "The question is recorded but is not interpreted by an LLM in this inspection policy.",
                "No ranking, causal inference, or root-cause confirmation was performed.",
                "This is a development-case preview centered on a known fault onset; it is not a detection benchmark.",
            ]
            for tool in ("incident_manifest", "incident_services", "incident_metrics", "incident_logs", "incident_traces"):
                reply = self.gateway.execute(token, tool, {"limit": 20, "max_bytes": 48000})
                result = reply["result"]
                ids = result["evidence_ids"]
                if ids:
                    observations.append({"text": f"{tool} returned {result['returned_count']} records out of {result['matched_count']} matching the recorded query. These records are observations, not a causal conclusion.", "evidence_ids": ids})
                if result["truncated"]:
                    unknowns.append(f"{tool} is truncated; inspect its saved query before treating this sample as representative.")
                unknowns.extend(result["limitations"])
            if self.store.begin_validation(run_id):
                self.store.complete_inspection(run_id, observations, list(dict.fromkeys(unknowns)))
        except GatewayDenied:
            self.store.fail_fixture(run_id, "INSPECTION_GATEWAY_DENIED")
        except Exception:
            logging.getLogger(__name__).exception("Read-only inspection failed for %s", run_id)
            self.store.fail_fixture(run_id, "INSPECTION_FAILED")
        finally:
            if token:
                self.gateway.revoke_capability(token)

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
