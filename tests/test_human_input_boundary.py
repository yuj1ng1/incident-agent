"""Untrusted model calls must not be confused with corrupt durable records."""
from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from investigation.models import SessionStatus as S
from investigation.scheduler import Scheduler
from investigation.state import LostClaim, RuntimeErrorBase
from investigation.worker import Worker
from runtime_support import FixtureAgent, call, create_runtime, create_session, response


UNTRUSTED_TEXT = "fixture-untrusted-sensitive-text"
INVALID_ARGUMENTS = [
    pytest.param('{"question":""}', id="empty"),
    pytest.param('{"question":"  \\t\\n"}', id="whitespace"),
    pytest.param('{"question":null}', id="null-question"),
    pytest.param('{"question":42}', id="integer-question"),
    pytest.param('{"question":true}', id="boolean-question"),
    pytest.param(json.dumps({"question": {"context": UNTRUSTED_TEXT}}), id="object-question"),
    pytest.param(json.dumps({"question": [UNTRUSTED_TEXT]}), id="array-question"),
    pytest.param(json.dumps({"question": "x" * 2001}), id="too-long"),
    pytest.param('{"question":"' + UNTRUSTED_TEXT, id="invalid-json"),
    pytest.param("{}", id="missing-question"),
    pytest.param(json.dumps({"question": "部署背景？", "extra": UNTRUSTED_TEXT}), id="extra-field"),
    pytest.param("null", id="null-json"),
    pytest.param("42", id="integer-json"),
    pytest.param('"question"', id="string-json"),
    pytest.param('["question"]', id="array-json"),
]


def human_response(arguments):
    item = call("request_human_input", {"question": "placeholder"}, "human-call")
    item["arguments"] = arguments
    return response(answer="", calls=[item])


@pytest.mark.parametrize("arguments", INVALID_ARGUMENTS)
def test_invalid_human_input_is_a_controlled_failure(tmp_path, arguments, caplog):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    agent = FixtureAgent([human_response(arguments)])

    result = Worker(manager, agent).run_once()

    assert result.session_id == session.session_id
    assert result.status == S.FAILED
    assert result.last_error_category == "invalid_human_request"
    assert result.owner_token is None and result.lease_until is None
    assert result.completed_at is not None
    assert (result.attempt_count, result.total_model_requests, result.total_tool_executions) == (1, 1, 1)
    assert result.human_request_count == 0
    assert manager.requests() == []
    checkpoint = manager.store.load_checkpoint(result)
    assert checkpoint["projected"]
    assert checkpoint["result"]["success"] is False
    incident = manager.incident_store.get_incident(session.incident_id)
    assert len(incident.investigations) == 1
    assert incident.consecutive_investigation_failures == 1
    # Check all retained checkpoints/events, not only the latest redacted view.
    with manager.store.read() as db:
        persisted = "".join(row[0] for table in (
            "investigation_sessions", "investigation_checkpoints", "investigation_events", "human_requests",
        ) for row in db.execute(f"SELECT data FROM {table}"))
    assert UNTRUSTED_TEXT not in persisted + caplog.text + manager.incident_store.file_path.read_text(encoding="utf-8")
    assert "store consistency failure" not in caplog.text


@pytest.mark.parametrize("question", ["部署背景？", "q", "x" * 2000], ids=["normal", "minimum", "maximum"])
def test_valid_question_waits_for_human_and_releases_claim(tmp_path, question):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    agent = FixtureAgent([human_response(json.dumps({"question": question}))])

    result = Worker(manager, agent).run_once()

    assert result.status == S.WAITING_HUMAN
    assert result.owner_token is None and result.lease_until is None
    assert result.human_request_count == 1
    requests = manager.requests()
    assert len(requests) == 1
    assert requests[0].session_id == session.session_id
    assert requests[0].question == question and requests[0].status == "pending"
    assert manager.store.load_checkpoint(result)["phase"] == "waiting_human"
    assert (result.total_model_requests, result.total_tool_executions) == (1, 1)


def test_redaction_expansion_is_a_controlled_failure(tmp_path):
    manager = create_runtime(tmp_path)
    create_session(manager)
    # A replacement such as token=a -> token=[redacted] can cross the stored
    # length limit even when the original question is exactly 2000 characters.
    question = "x" * 1993 + "token=a"
    assert len(question) == 2000
    agent = FixtureAgent([human_response(json.dumps({"question": question}))])

    result = Worker(manager, agent).run_once()

    assert result.status == S.FAILED
    assert result.last_error_category == "invalid_human_request"
    assert result.owner_token is None and result.lease_until is None
    assert manager.requests() == []


@pytest.mark.parametrize("question", ["", " \t\n", None, 42, {}, "x" * 2001],
                         ids=["empty", "whitespace", "null", "integer", "object", "too-long"])
def test_manager_rejects_invalid_question_without_durable_changes(tmp_path, question):
    manager = create_runtime(tmp_path)
    create_session(manager)
    claimed = manager.store.claim("test-worker", manager.limits)
    before = manager.store.path.read_bytes()

    with pytest.raises(RuntimeErrorBase, match="^invalid_human_request$"):
        manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), question,
                              manager.store.load_checkpoint(claimed))

    assert manager.requests() == []
    assert manager.store.get(claimed.session_id) == claimed
    assert manager.store.path.read_bytes() == before


def test_manager_keeps_fencing_for_invalid_input(tmp_path):
    manager = create_runtime(tmp_path)
    create_session(manager)
    claimed = manager.store.claim("test-worker", manager.limits)
    with pytest.raises(LostClaim):
        manager.request_human(claimed.session_id, ("stale-worker", claimed.fencing_token), "",
                              manager.store.load_checkpoint(claimed))
    assert manager.requests() == []


@pytest.mark.parametrize("arguments", ['{"question":""}', '{"question":'], ids=["validation", "json-decode"])
def test_scheduler_continues_with_other_incident_after_invalid_call(tmp_path, arguments, caplog):
    manager = create_runtime(tmp_path)
    bad = create_session(manager)
    other = create_session(manager, service="inventory-service")
    agent = FixtureAgent([human_response(arguments), response()])
    scheduler = Scheduler(Worker(manager, agent), interval=0)
    tick = scheduler.tick
    ticks = 0

    def bounded_tick():
        nonlocal ticks
        ticks += 1
        try:
            return tick()
        finally:
            if ticks >= 2:
                scheduler.stop_requested.set()

    with patch.object(scheduler, "tick", side_effect=bounded_tick):
        scheduler.run()

    assert ticks == 2
    assert manager.store.get(bad.session_id).status == S.FAILED
    assert manager.store.get(other.session_id).status == S.COMPLETED
    assert agent.request_durable_response.call_count == 2
    assert manager.requests() == []
    assert "store consistency failure" not in caplog.text


@pytest.mark.parametrize("corruption", ["sqlite", "checkpoint-json", "checkpoint-validation", "session-validation"])
def test_scheduler_still_stops_on_real_durable_corruption(tmp_path, corruption, caplog):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    other = create_session(manager, service="inventory-service")
    if corruption == "sqlite":
        manager.store.path.write_bytes(b"not SQLite")
    else:
        with manager.store.transaction() as db:
            current = manager.store.get(session.session_id, db)
            manager.store.checkpoint(current, manager.store.load_checkpoint(current, db), db)
            manager.store.save(current, db)
        with closing(sqlite3.connect(manager.store.path)) as db, db:
            if corruption == "session-validation":
                data = current.model_dump(mode="json") | {"human_request_count": -1}
                db.execute("UPDATE investigation_sessions SET data=? WHERE session_id=?",
                           (json.dumps(data), session.session_id))
            else:
                data = '{"phase":' if corruption == "checkpoint-json" else json.dumps({
                    "schema_version": 1, "phase": "invalid",
                })
                db.execute("UPDATE investigation_checkpoints SET data=? WHERE checkpoint_id=?",
                           (data, current.checkpoint_id))
    agent = FixtureAgent()
    scheduler = Scheduler(Worker(manager, agent), interval=0)
    tick = scheduler.tick

    def bounded_tick():
        result = tick()
        scheduler.stop_requested.set()
        return result

    with patch.object(scheduler, "tick", side_effect=bounded_tick):
        scheduler.run()

    assert scheduler.stop_requested.is_set()
    assert "Runtime stopped; store consistency failure" in caplog.text
    agent.request_durable_response.assert_not_called()
    if corruption != "sqlite":
        assert manager.store.get(other.session_id).status == S.QUEUED


def test_manager_does_not_mask_persisted_session_validation_error(tmp_path):
    manager = create_runtime(tmp_path)
    create_session(manager)
    claimed = manager.store.claim("test-worker", manager.limits)
    with closing(sqlite3.connect(manager.store.path)) as db, db:
        db.execute("UPDATE investigation_sessions SET data=? WHERE session_id=?", (
            json.dumps(claimed.model_dump(mode="json") | {"human_request_count": -1}), claimed.session_id,
        ))
    with pytest.raises(ValidationError):
        manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), "部署背景？", {})
