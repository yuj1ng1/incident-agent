"""Startup retry policy only; these tests do not spawn child processes."""
from unittest.mock import Mock, patch

import pytest

import runtime_process_fixture as fixture
from storage_lock import FileLockError


def test_claim_startup_retries_only_transient_file_lock():
    manager = object()
    create = Mock(side_effect=[FileLockError("busy"), manager])
    with patch.object(fixture, "create_runtime", create), \
         patch.object(fixture.time, "monotonic", side_effect=[0, 1]), \
         patch.object(fixture.time, "sleep") as sleep:
        assert fixture.create_claim_runtime("isolated-root") is manager
    assert create.call_count == 2
    sleep.assert_called_once_with(0.002)


def test_claim_startup_persistent_file_lock_stops_at_deadline():
    error = FileLockError("busy")
    create = Mock(side_effect=error)
    with patch.object(fixture, "create_runtime", create), \
         patch.object(fixture.time, "monotonic", side_effect=[0, 11]), \
         patch.object(fixture.time, "sleep") as sleep:
        with pytest.raises(FileLockError) as caught:
            fixture.create_claim_runtime("isolated-root")
    assert caught.value is error
    create.assert_called_once_with("isolated-root")
    sleep.assert_not_called()


def test_claim_startup_does_not_retry_configuration_or_data_errors():
    error = ValueError("invalid configuration")
    create = Mock(side_effect=error)
    with patch.object(fixture, "create_runtime", create), \
         patch.object(fixture.time, "sleep") as sleep:
        with pytest.raises(ValueError) as caught:
            fixture.create_claim_runtime("isolated-root")
    assert caught.value is error
    create.assert_called_once_with("isolated-root")
    sleep.assert_not_called()
