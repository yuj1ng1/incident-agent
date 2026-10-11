"""Entry points select the same stores independently of the caller's directory."""
from pathlib import Path
from unittest.mock import patch

import pytest

from main import main as monitor_main
from runtime_cli import main as runtime_main
from web_api.queries import RuntimeQueries


@pytest.mark.parametrize("entry", ["monitor", "cli"])
def test_incident_path_matches_web_defaults_from_another_directory(tmp_path, monkeypatch, entry):
    monkeypatch.chdir(tmp_path)
    expected = Path(__file__).resolve().parents[1] / "data" / "incidents.json"
    assert RuntimeQueries().incident_path == expected

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
    incidents.assert_called_once_with(expected if entry == "monitor" else str(expected))
    if entry == "cli":
        runtime.assert_called_once_with(str(expected.with_name("runtime.sqlite3")))
    assert list(tmp_path.iterdir()) == []
