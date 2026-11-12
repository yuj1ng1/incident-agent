"""Entry points select the same stores independently of the caller's directory."""
from pathlib import Path
from unittest.mock import patch

import pytest

from main import main as monitor_main
from runtime_cli import main as runtime_main
from web_api.queries import RuntimeQueries


@pytest.mark.parametrize("entry", ["monitor", "cli"])
def test_incident_url_matches_web_defaults_from_another_directory(tmp_path, monkeypatch, entry):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INCIDENT_AGENT_MODEL_MODE", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    assert RuntimeQueries().incident_url is None

    class StoreIntercepted(Exception):
        pass

    with patch("dotenv.load_dotenv"), patch("openai.OpenAI"), \
            patch("investigation.store.RuntimeStore") as runtime, \
            patch("incident_store.IncidentStore", side_effect=StoreIntercepted) as incidents:
        with pytest.raises(StoreIntercepted):
            if entry == "monitor":
                monitor_main()
            else:
                runtime_main(["list-sessions"])
    incidents.assert_called_once_with() if entry == "monitor" else incidents.assert_called_once_with(None)
    if entry == "cli":
        runtime.assert_called_once_with(None)
    assert list(tmp_path.iterdir()) == []
