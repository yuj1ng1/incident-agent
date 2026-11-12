"""Keep parent and child regression logs out of the operator's real log."""
import logging
import os
from pathlib import Path
import tempfile
from functools import wraps

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


_logs = tempfile.TemporaryDirectory(prefix="incident-agent-pytest-logs-")
_previous_log_path = os.environ.get("INCIDENT_AGENT_LOG_PATH")
os.environ["INCIDENT_AGENT_LOG_PATH"] = str(Path(_logs.name) / "agent.log")


@pytest.fixture(autouse=True)
def protect_operator_stores(monkeypatch):
    """An omitted or real path must fail before opening operator data."""
    from incident_store import IncidentStore
    from investigation.store import RuntimeStore
    from mysql_support import incident_database_url
    from remediation.store import RemediationStore

    protected = Path(__file__).resolve().parents[1] / "data"

    incident_original = IncidentStore.__init__

    @wraps(incident_original)
    def isolated_incident(self, location=None, *args, **kwargs):
        path = location
        if path is not None and not str(path).startswith("mysql+"):
            path = Path(path)
            if path.resolve().is_relative_to(protected):
                raise AssertionError("tests_must_use_temporary_store_paths")
            location = incident_database_url(path)
        return incident_original(self, location, *args, **kwargs)

    monkeypatch.setattr(IncidentStore, "__init__", isolated_incident)

    remediation_original = RemediationStore.__init__

    @wraps(remediation_original)
    def isolated_remediation(self, file_path=None, *args, **kwargs):
        path = file_path if file_path is not None else kwargs.get("file_path")
        if path is not None:
            path = Path(path)
            if path.resolve().is_relative_to(protected):
                raise AssertionError("tests_must_use_temporary_store_paths")
        return remediation_original(self, file_path, *args, **kwargs)

    monkeypatch.setattr(RemediationStore, "__init__", isolated_remediation)

    original = RuntimeStore.__init__

    @wraps(original)
    def isolated(self, database_url=None, *args, **kwargs):
        resolved = database_url or os.environ.get("INCIDENT_AGENT_DATABASE_URL")
        if not resolved:
            raise AssertionError("tests_require_isolated_mysql_url")
        try:
            database = make_url(str(resolved)).database or ""
        except ArgumentError:
            raise AssertionError("tests_must_pass_mysql_url_not_a_path") from None
        if not database.startswith("ia_test_"):
            raise AssertionError("tests_must_use_isolated_mysql_schema")
        return original(self, database_url, *args, **kwargs)

    monkeypatch.setattr(RuntimeStore, "__init__", isolated)


@pytest.fixture(autouse=True)
def release_test_database_connections():
    """Each test owns a schema URL; do not keep one pool per historical URL."""
    yield
    from db.config import dispose_engines

    dispose_engines()


@pytest.fixture(scope="session", autouse=True)
def isolated_default_database(tmp_path_factory):
    """Entry points using the default URL must still target an isolated schema."""
    from mysql_support import TEST_DATABASE_URL_ENV, runtime_database_url

    if not os.environ.get(TEST_DATABASE_URL_ENV):
        yield
        return
    previous = os.environ.get("INCIDENT_AGENT_DATABASE_URL")
    os.environ["INCIDENT_AGENT_DATABASE_URL"] = runtime_database_url(
        tmp_path_factory.mktemp("default-store")
    )
    yield
    if previous is None:
        os.environ.pop("INCIDENT_AGENT_DATABASE_URL", None)
    else:
        os.environ["INCIDENT_AGENT_DATABASE_URL"] = previous


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_databases(isolated_default_database):
    yield
    from mysql_support import drop_test_databases

    drop_test_databases()


def pytest_unconfigure(config):
    logging.shutdown()
    _logs.cleanup()
    if _previous_log_path is None:
        os.environ.pop("INCIDENT_AGENT_LOG_PATH", None)
    else:
        os.environ["INCIDENT_AGENT_LOG_PATH"] = _previous_log_path
