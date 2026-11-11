"""Isolated MySQL schemas for RuntimeStore tests; never the operator database."""
import hashlib
import os
from pathlib import Path
import socket
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from db.config import dispose_engines
from db.tables import create_runtime_schema

TEST_DATABASE_URL_ENV = "INCIDENT_AGENT_TEST_DATABASE_URL"
SCHEMA_PREFIX = "ia_test_"

_created = set()
_urls = {}


def test_database_url():
    value = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not value:
        raise RuntimeError("test_database_url_not_configured")
    url = make_url(value)
    if url.drivername != "mysql+pymysql" or not url.database:
        raise RuntimeError("test_database_url_must_be_mysql")
    return url


@contextmanager
def allow_isolated_mysql_sockets(forbidden):
    """Block every TCP target except the configured temporary MySQL server."""
    url = test_database_url()
    destination = (url.host or "localhost", url.port or 3306)
    addresses = {info[4] for info in socket.getaddrinfo(*destination, type=socket.SOCK_STREAM)}
    create = socket.create_connection
    connect = socket.socket.connect

    def guarded_create(address, *args, **kwargs):
        if address == destination or address in addresses:
            return create(address, *args, **kwargs)
        return forbidden(address)

    def guarded_connect(sock, address):
        if address in addresses:
            return connect(sock, address)
        return forbidden(address)

    with patch("socket.create_connection", guarded_create), patch("socket.socket.connect", guarded_connect):
        yield


def _admin_engine():
    return create_engine(test_database_url(), poolclass=NullPool)


def _schema_name(kind, root):
    key = str(Path(root).resolve())
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return SCHEMA_PREFIX + kind + digest


def _create_database(name):
    admin = _admin_engine()
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS `{name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
            )
    finally:
        admin.dispose()
    _created.add(name)


def runtime_database_url(root):
    """Stable schema per test root so restart checks reuse the same database."""
    key = str(Path(root).resolve())
    cached = _urls.get(key)
    if cached is not None:
        return cached
    base = test_database_url()
    name = _schema_name("", root)
    _create_database(name)
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, poolclass=NullPool)
    try:
        if not inspect(engine).has_table("investigation_sessions"):
            create_runtime_schema(engine)
    finally:
        engine.dispose()
    _urls[key] = url
    return url


def empty_database_url(root):
    """Schema without tables, for explicit not-migrated failure tests."""
    base = test_database_url()
    name = _schema_name("empty_", root)
    _create_database(name)
    return base.set(database=name).render_as_string(hide_password=False)


def drop_test_databases():
    _urls.clear()
    if not _created or not os.environ.get(TEST_DATABASE_URL_ENV):
        return
    dispose_engines()
    admin = _admin_engine()
    try:
        with admin.begin() as connection:
            for name in sorted(_created):
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
    finally:
        admin.dispose()
        _created.clear()
