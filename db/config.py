"""Database URL resolution and process-wide SQLAlchemy engine pool."""
import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError

DATABASE_URL_ENV = "INCIDENT_AGENT_DATABASE_URL"
REQUIRED_DRIVER = "mysql+pymysql"

_engines: dict[str, Engine] = {}


def database_url():
    """Return the explicitly configured MySQL URL; never fall back to a file DB."""
    value = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not value:
        raise RuntimeError("database_url_not_configured")
    try:
        url = make_url(value)
    except ArgumentError:
        raise RuntimeError("database_url_invalid") from None
    if url.drivername != REQUIRED_DRIVER or not url.database:
        raise RuntimeError("mysql_pymysql_database_url_required")
    return value


def get_engine(url=None):
    """Return a pooled engine for this process, keyed by the resolved URL."""
    resolved = url or database_url()
    engine = _engines.get(resolved)
    if engine is None:
        engine = create_engine(
            resolved,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
            pool_timeout=30,
            isolation_level="READ COMMITTED",
            connect_args={"init_command": "SET time_zone='+00:00'"},
        )
        _engines[resolved] = engine
    return engine


def dispose_engines():
    """Close pooled connections, for CLI shutdown and isolated tests."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
