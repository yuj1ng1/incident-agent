"""These parser/transaction contract tests intentionally require no MySQL server."""
import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_default_database():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_databases():
    yield


@pytest.fixture(autouse=True)
def protect_operator_stores(monkeypatch):
    # Exercise the real URL validation rather than the legacy path-mapping shim.
    monkeypatch.setenv(
        "INCIDENT_AGENT_DATABASE_URL",
        "mysql+pymysql://unit@127.0.0.1/ia_test_offline",
    )
    yield
