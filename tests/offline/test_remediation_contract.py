"""Offline transaction contracts; these do not attest to MySQL row locking."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

from db import tables
from models.remediation import RemediationProposal
from remediation.store import RemediationStore, StoreError, empty_document


@pytest.fixture
def store(monkeypatch, tmp_path):
    # SQLite is only a SQLAlchemy transaction test double. Production still
    # accepts mysql+pymysql URLs exclusively, and all locking tests use MySQL.
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE remediation_state (id INTEGER PRIMARY KEY, "
            "revision BIGINT NOT NULL, data TEXT NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.execute(tables.remediation_state.insert().values(
            id=1, revision=0, data=json.dumps(empty_document()),
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
    monkeypatch.setattr("remediation.store.get_engine", lambda url: engine)
    store = RemediationStore("mysql+pymysql://unit@127.0.0.1/ia_test_offline")
    yield store
    engine.dispose()


def proposal():
    return RemediationProposal(
        proposal_id="rem-offline", incident_id="inc-offline", service="order-service",
        summary="fixture", reason="fixture", action_type="disable_demo_regression",
        parameters={"service": "order-service"}, risk="low",
        expected_commit_sha="a" * 40,
        expected_process_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def row(store):
    with store.engine.connect() as connection:
        return dict(connection.execute(select(tables.remediation_state)).mappings().one())


def test_proposal_approval_and_audit_commit_as_one_document(store):
    value = store.create(proposal())
    store.approve(value.proposal_id, "operator")
    saved = row(store)
    data = json.loads(saved["data"])
    assert saved["revision"] == 2
    assert data["proposals"][value.proposal_id]["approved_by"] == "operator"
    assert [item["event"] for item in data["audit"]] == ["proposal_created", "approved"]


def test_exception_after_sql_update_rolls_back_document_revision_and_audit(store):
    store.create(proposal())
    before = row(store)
    save = store._save

    def failed_commit(data):
        save(data)
        raise StoreError("commit_failed")

    with patch.object(store, "_save", failed_commit), pytest.raises(StoreError):
        store.approve("rem-offline", "operator")
    assert row(store) == before
    store.approve("rem-offline", "operator")
    assert row(store)["revision"] == before["revision"] + 1


def test_readonly_transaction_fails_before_opening_any_connection(store):
    readonly = RemediationStore(store.database_url, read_only=True)
    before = row(store)
    readonly.load()
    with pytest.raises(StoreError, match="read_only"):
        with readonly.transaction():
            raise AssertionError("must not yield")
    assert row(store) == before


def test_save_requires_this_contexts_active_transaction(store):
    with pytest.raises(StoreError, match="transaction_required"):
        store._save(empty_document())
    with store.transaction():
        with pytest.raises(StoreError, match="nested_transaction"):
            with store.transaction():
                pass


def test_corrupt_document_fails_closed_without_repair_or_revision_change(store):
    with store.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE remediation_state SET data='{broken'")
    before = row(store)
    with pytest.raises(StoreError, match="corrupt"):
        with store.transaction():
            pass
    assert row(store) == before


def test_missing_singleton_fails_closed(store):
    with store.engine.begin() as connection:
        connection.execute(tables.remediation_state.delete())
    with pytest.raises(StoreError, match="unavailable"):
        store.load()


def test_v1_read_does_not_rewrite_and_next_write_upgrades_atomically(store):
    store.create(proposal())
    legacy = store.load()
    legacy["version"] = 1
    for value in legacy["proposals"].values():
        value.pop("compensation")
        value.pop("incident_fingerprint")
    with store.engine.begin() as connection:
        connection.execute(tables.remediation_state.update().values(data=json.dumps(legacy)))
    before = row(store)
    assert store.load() == legacy
    assert row(store) == before
    store.approve("rem-offline", "operator")
    assert store.load()["version"] == 2


def test_v1_optional_audit_details_support_reads_and_idempotent_commands(store):
    from remediation.approval import approval_digest
    from web_api.remediation_views import detail_view

    saved = store.create(proposal())
    legacy = store.load()
    legacy["version"] = 1
    legacy["audit"][0].pop("details")
    with store.engine.begin() as connection:
        connection.execute(tables.remediation_state.update().values(data=json.dumps(legacy)))
    before = row(store)
    view = detail_view(store, store.load(), saved)
    assert view.audit[0].reason is None
    assert row(store) == before
    assessment = store.request_compensation(
        saved.proposal_id,
        "requester",
        "review legacy proposal",
        "legacy-compensation-request",
    )
    assert assessment.status == "blocked"
    for _ in range(2):
        store.approve(
            saved.proposal_id,
            "operator",
            command_id="legacy-approval",
            expected_digest=approval_digest(saved),
        )
    assert sum(event["event"] == "approved" for event in store.load()["audit"]) == 1
    assert "details" not in store.load()["audit"][0]


def test_mysql_query_really_requests_a_row_lock():
    statement = select(tables.remediation_state).where(
        tables.remediation_state.c.id == 1,
    ).with_for_update()
    assert "FOR UPDATE" in str(statement.compile(dialect=mysql.dialect()))


def test_production_store_rejects_file_and_sqlite_urls():
    for location in ("legacy.json", "sqlite:///legacy.db", "mysql://user@host/db"):
        with pytest.raises(StoreError, match="database_url_required"):
            RemediationStore(database_url=location)


class LockConnection:
    """Inject server replies without pretending to implement MySQL locking."""

    def __init__(self, *, acquire=1, release=1):
        self.acquire = acquire
        self.release = release
        self.invalidated = False
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def exec_driver_sql(self, sql, parameters):
        self.calls.append((sql, parameters))
        reply = self.acquire if "GET_LOCK" in sql else self.release
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(scalar=lambda: reply)

    def invalidate(self):
        self.invalidated = True


@pytest.mark.parametrize("reply,code,invalidated", (
    (0, "service_busy", False),
    (None, "store_unavailable", True),
    (SQLAlchemyError("lost acquisition acknowledgement"), "store_unavailable", True),
))
def test_lock_acquisition_failure_never_yields_or_reuses_uncertain_session(
    store, reply, code, invalidated,
):
    connection = LockConnection(acquire=reply)
    store.engine = SimpleNamespace(connect=lambda: connection)
    with pytest.raises(StoreError, match=code):
        with store.service_lock("fixture-service"):
            pytest.fail("an unconfirmed lock must never authorize an action")
    assert connection.invalidated is invalidated
    assert len(connection.calls) == 1


def test_lock_is_released_when_action_body_raises(store):
    connection = LockConnection()
    store.engine = SimpleNamespace(connect=lambda: connection)
    with pytest.raises(ValueError, match="action failed"):
        with store.service_lock("fixture-service"):
            raise ValueError("action failed")
    assert [sql for sql, _ in connection.calls] == [
        "SELECT GET_LOCK(%s, %s)", "SELECT RELEASE_LOCK(%s)",
    ]
    assert connection.calls[0][1][0] == connection.calls[1][1][0]
    assert len(connection.calls[0][1][0]) <= 64
    assert not connection.invalidated


@pytest.mark.parametrize("reply", (0, None, SQLAlchemyError("lost release reply")))
def test_uncertain_lock_release_invalidates_pooled_session(store, reply):
    connection = LockConnection(release=reply)
    store.engine = SimpleNamespace(connect=lambda: connection)
    with pytest.raises(StoreError):
        with store.service_lock("fixture-service"):
            pass
    assert connection.invalidated


@pytest.mark.parametrize("timeout", (-1, None, float("inf")))
def test_lock_timeout_is_finite_and_validated_before_connecting(store, timeout):
    def unexpected_connection():
        pytest.fail("invalid timeout must fail before opening a connection")

    store.engine = SimpleNamespace(connect=unexpected_connection)
    with pytest.raises(StoreError, match="lock_timeout_invalid"):
        with store.service_lock("fixture-service", timeout=timeout):
            pass
