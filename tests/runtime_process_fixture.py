"""Only this dedicated child exits abruptly; never terminates another process."""
import os
import json
from datetime import datetime
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_support import FixtureAgent, SHA, call, create_runtime, response
from investigation.state import LostClaim
from investigation.worker import Worker
from storage_lock import FileLockError


def create_claim_runtime(root):
    """Allow bounded startup contention before the claim ready barrier."""
    deadline = time.monotonic() + 10
    while True:
        try:
            return create_runtime(root)
        except FileLockError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.002)


if __name__ == "__main__":
    phase = sys.argv[2]
    manager = (
        create_claim_runtime(sys.argv[1])
        if phase == "claim-contender"
        else create_runtime(sys.argv[1])
    )
    if phase == "claim-contender":
        owner = sys.argv[3]
        directory = Path(sys.argv[1])
        (directory / ("ready-" + owner)).write_text("ready")
        deadline = time.monotonic() + 30
        while not (directory / "claim-release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("claim_barrier_timeout")
            time.sleep(0.02)
        claim = manager.store.claim(owner, manager.limits)
        (directory / ("claim-" + owner + ".json")).write_text(json.dumps(
            None if claim is None else {"session_id": claim.session_id, "fence": claim.fencing_token}
        ))
        if claim is not None:
            deadline = time.monotonic() + 30
            while not (directory / "fence-release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("fence_barrier_timeout")
                time.sleep(0.02)
            manager.store.clock = lambda: datetime.fromisoformat((directory / "clock.txt").read_text())
            try:
                with manager.store.transaction() as db:
                    session = manager.store.guard(db, claim.session_id, owner, claim.fencing_token)
                    manager.store.save(session, db)
            except LostClaim:
                result = "old_owner_fenced"
            else:
                result = "unsafe_stale_write"
            (directory / "fence-result").write_text(result)
        raise SystemExit(0)

    source_call = call("get_source_file", {"service": "order-service", "path": "app.py", "ref": SHA})
    agent = FixtureAgent([response(answer="", calls=[source_call]), response()])
    original = agent.run_fixture_tool
    def recorded_tool(item, **kwargs):
        result = original(item, **kwargs)
        with (Path(sys.argv[1]) / "tool-io.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"name": item.name, "call_id": item.call_id}) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return result
    agent.tool_manager.run_tool = recorded_tool
    def boundary(name, session_id):
        if name == phase:
            if phase == "tool_before_io" and len(agent.executions) < 1:
                return
            if phase in {"tool_after_io", "tool_checkpoint_committed"} and len(agent.executions) < 2:
                return
            os._exit(23)
    if phase == "wait-model":
        count = 0
        def waiting_response(*args, **kwargs):
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
