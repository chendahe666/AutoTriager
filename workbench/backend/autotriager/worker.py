"""Single-worker synthetic queue. No network, engine, credentials or model."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time


class FixtureWorker:
    def __init__(self, store, stage_delay=0.3):
        self.store = store
        self.stage_delay = stage_delay
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autotriager-fixture")
        self._lock = threading.Lock()
        self._submitted = set()
        self._closed = False

    def submit(self, run_id):
        with self._lock:
            if self._closed:
                raise RuntimeError("Fixture worker is closed")
            if run_id in self._submitted:
                return None
            self._submitted.add(run_id)
            return self._executor.submit(self._execute, run_id)

    def _execute(self, run_id):
        try:
            time.sleep(self.stage_delay)
            if not self.store.start_fixture(run_id):
                return
            for modality in ("metrics", "traces", "logs"):
                time.sleep(self.stage_delay)
                if not self.store.persist_fixture_query(run_id, modality):
                    return
            if not self.store.begin_validation(run_id):
                return
            time.sleep(self.stage_delay)
            self.store.complete_fixture(run_id)
        except Exception:
            logging.getLogger(__name__).exception("Synthetic fixture worker failed for %s", run_id)
            self.store.fail_fixture(run_id)

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
