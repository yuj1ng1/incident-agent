"""The store must never silently fall back to the legacy file databases."""
import pytest

from db.config import DATABASE_URL_ENV, database_socket_addresses, database_url

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


def test_database_socket_addresses_bind_dns_results_to_the_configured_port(monkeypatch):
    import socket

    def resolve(host, port, *, type):
        assert (host, port, type) == ("mysql", 3306, socket.SOCK_STREAM)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.25.0.4", 3306)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::4", 3306, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    addresses = database_socket_addresses("mysql+pymysql://user:fixture@mysql/db")
    assert addresses == {("mysql", 3306), ("172.25.0.4", 3306), ("fd00::4", 3306)}
    assert ("172.25.0.4", 3307) not in addresses
    assert ("127.0.0.1", 3306) not in addresses
