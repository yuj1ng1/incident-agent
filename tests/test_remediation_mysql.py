"""Real MySQL advisory sessions, including process and connection failures."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from mysql_support import empty_database_url, remediation_database_url
from remediation.store import RemediationStore, StoreError


def lock_key(name):
    return "ia_remediation_" + hashlib.sha256(name.encode()).hexdigest()[:48]


def assert_lock_released(observer, name):
    # KILL and client disconnect return before server-side cleanup completes.
    # Keep the release assertion, allowing only a bounded cleanup interval.
    deadline = time.monotonic() + 5
    while True:
        free = observer.exec_driver_sql(
            "SELECT IS_FREE_LOCK(%s)", (lock_key(name),),
        ).scalar_one()
        if free == 1:
            return
        assert time.monotonic() < deadline, "server did not release the lost session lock"
        time.sleep(0.01)


def test_execution_lock_serializes_processes_even_with_different_schemas(tmp_path):
    first = RemediationStore(remediation_database_url(tmp_path / "first"))
    second = RemediationStore(empty_database_url(tmp_path / "second"))
    name = str(tmp_path / "shared-service")
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("remediation_process_fixture.py")),
            str(tmp_path),
            "hold-service-lock",
            name,
        ],
        env=dict(os.environ, INCIDENT_AGENT_DATABASE_URL=first.database_url),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = tmp_path / "service-lock-ready"
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child did not acquire the database execution lock"
        with pytest.raises(StoreError, match="service_busy"):
            with second.service_lock(name):
                pytest.fail("another process already owns this service")
        # An unrelated service remains available on the same MySQL server.
        with second.service_lock(name + "-independent"):
            pass
        child.communicate(input="release\n", timeout=10)
        assert child.returncode == 0
        with second.service_lock(name):
            pass
    finally:
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=5)


def test_disconnected_lock_session_fails_closed_and_is_not_reused(tmp_path):
    store = RemediationStore(remediation_database_url(tmp_path / "store"))
    name = str(tmp_path / "disconnected-service")
    with store.engine.connect() as observer:
        with pytest.raises(StoreError, match="store_unavailable"):
            with store.service_lock(name):
                owner = observer.exec_driver_sql(
                    "SELECT IS_USED_LOCK(%s)",
                    (lock_key(name),),
                ).scalar_one()
                assert type(owner) is int
                # The test account kills only its own identified MySQL session.
                observer.exec_driver_sql(f"KILL CONNECTION {owner}")
        assert_lock_released(observer, name)
    with store.engine.connect() as fresh:
        assert fresh.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one() != owner
    with store.service_lock(name):
        pass


@pytest.mark.parametrize("statement", ("GET_LOCK", "RELEASE_LOCK"))
def test_lost_lock_reply_discards_the_actual_server_session(tmp_path, monkeypatch, statement):
    store = RemediationStore(remediation_database_url(tmp_path / statement))
    name = str(tmp_path / "uncertain-service")
    original = Connection.exec_driver_sql
    observed = []

    def lose_reply(connection, sql, *args, **kwargs):
        result = original(connection, sql, *args, **kwargs)
        if statement in sql:
            assert result.scalar_one() == 1
            observed.append(True)
            # SQL reached MySQL; simulate losing only its acknowledgement.
            raise OperationalError(sql, None, OSError("test reply lost"))
        return result

    with monkeypatch.context() as failure:
        failure.setattr(Connection, "exec_driver_sql", lose_reply)
        with pytest.raises(StoreError, match="store_unavailable"):
            with store.service_lock(name):
                assert statement == "RELEASE_LOCK"
    assert observed == [True]
    with store.engine.connect() as observer:
        assert_lock_released(observer, name)
    with store.service_lock(name):
        pass
