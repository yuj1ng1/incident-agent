"""Keep parent and child regression logs out of the operator's real log."""
import logging
import os
from pathlib import Path
import tempfile


_logs = tempfile.TemporaryDirectory(prefix="incident-agent-pytest-logs-")
_previous_log_path = os.environ.get("INCIDENT_AGENT_LOG_PATH")
os.environ["INCIDENT_AGENT_LOG_PATH"] = str(Path(_logs.name) / "agent.log")


def pytest_unconfigure(config):
    logging.shutdown()
    _logs.cleanup()
    if _previous_log_path is None:
        os.environ.pop("INCIDENT_AGENT_LOG_PATH", None)
    else:
        os.environ["INCIDENT_AGENT_LOG_PATH"] = _previous_log_path
