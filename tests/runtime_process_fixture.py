"""Only this dedicated child exits abruptly; never terminates another process."""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_support import FixtureAgent, call, create_runtime, response
from investigation.worker import Worker


if __name__ == "__main__":
    manager = create_runtime(sys.argv[1])
    phase = sys.argv[2]
    def boundary(name, session_id):
        if name == phase:
            os._exit(23)
    agent = FixtureAgent([response(answer="", calls=[call()]), response()])
    if phase == "wait-model":
        count = 0
        def waiting_response(*args):
            global count
            count += 1
            if count > 1:
                return response()
            (Path(sys.argv[1]) / "ready").write_text("request in flight")
            deadline = time.monotonic() + 20
            while not (Path(sys.argv[1]) / "release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                time.sleep(0.05)
            return response(answer="", calls=[call()])
        agent.request_durable_response.side_effect = waiting_response
    Worker(manager, agent, boundary).run_once()
