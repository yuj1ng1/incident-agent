"""The store must never silently fall back to the legacy file databases."""
import pytest

from db.config import DATABASE_URL_ENV, database_url

MYSQL_URL = (
    "mysql+pymysql://incident_agent:secret@127.0.0.1:3306/"
    "incident_agent?charset=utf8mb4"
)


def test_database_url_requires_an_explicit_mysql_url(monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    with pytest.raises(RuntimeError, match="database_url_not_configured"):
        database_url()

    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:///data/incidents.json")
    with pytest.raises(RuntimeError, match="mysql_pymysql_database_url_required"):
        database_url()

    monkeypatch.setenv(DATABASE_URL_ENV, "not-a-url")
    with pytest.raises(RuntimeError, match="database_url_invalid"):
        database_url()

    monkeypatch.setenv(DATABASE_URL_ENV, MYSQL_URL)
    assert database_url() == MYSQL_URL
