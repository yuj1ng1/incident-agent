"""Keep parent and child regression logs out of the operator's real log."""
import logging
import os
from pathlib import Path
import tempfile
from functools import wraps

import pytest


_logs = tempfile.TemporaryDirectory(prefix="incident-agent-pytest-logs-")
_previous_log_path = os.environ.get("INCIDENT_AGENT_LOG_PATH")
os.environ["INCIDENT_AGENT_LOG_PATH"] = str(Path(_logs.name) / "agent.log")


@pytest.fixture(autouse=True)
def protect_operator_stores(monkeypatch):
    """An omitted path must fail before opening any real project runtime data."""
    from incident_store import IncidentStore
    from investigation.store import DEFAULT_STORE, RuntimeStore
    from remediation.store import DEFAULT_STORE as REMEDIATION_STORE, RemediationStore

    protected = Path(__file__).resolve().parents[1] / "data"

    def guard_constructor(store_class, default_path=None):
        original = store_class.__init__

        @wraps(original)
        def isolated(self, *args, **kwargs):
            path = args[0] if args else kwargs.get("path", kwargs.get("file_path", default_path))
            if path is not None and Path(path).resolve().is_relative_to(protected):
                raise AssertionError("tests_must_use_temporary_store_paths")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(store_class, "__init__", isolated)

    guard_constructor(IncidentStore)
    guard_constructor(RuntimeStore, DEFAULT_STORE)
    guard_constructor(RemediationStore, REMEDIATION_STORE)


def pytest_unconfigure(config):
    logging.shutdown()
    _logs.cleanup()
    if _previous_log_path is None:
        os.environ.pop("INCIDENT_AGENT_LOG_PATH", None)
    else:
        os.environ["INCIDENT_AGENT_LOG_PATH"] = _previous_log_path
