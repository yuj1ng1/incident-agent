"""One background Worker leaves monitoring free while a session waits for humans."""
import logging
import threading


class Scheduler:
    def __init__(self, worker, interval=1):
        self.worker = worker
        self.interval = interval
        self.stop_requested = threading.Event()
        self.worker.stop_requested = self.stop_requested.is_set
        self.thread = None

    def tick(self):
        return self.worker.run_once()

    def run(self):
        while not self.stop_requested.is_set():
            try:
                result = self.tick()
            except Exception:
                logging.getLogger(__name__).exception("Runtime scheduler failed; durable state retained")
                result = None
            if result is None:
                self.stop_requested.wait(self.interval)

    def start(self):
        if self.thread is not None:
            raise RuntimeError("scheduler_already_started")
        self.thread = threading.Thread(target=self.run, name="investigation-worker", daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join()
