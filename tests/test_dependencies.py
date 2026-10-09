"""Exercise the SDK's real transport types without network access or credentials."""
from importlib import metadata, util
import os
from pathlib import Path
import unittest

import httpx2
import openai


class DependencyTests(unittest.TestCase):
    def test_sdk_uses_native_httpx2_request_response_and_error(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx2.Response(402, json={"error": {"message": "test blocked"}})

        with httpx2.Client(transport=httpx2.MockTransport(respond), trust_env=False) as transport:
            with openai.OpenAI(api_key="fixture-only", http_client=transport, max_retries=0) as client:
                with self.assertRaises(openai.APIStatusError) as raised:
                    client.responses.create(model="fixture", input="fixture")
        self.assertEqual(len(requests), 1)
        self.assertIsInstance(requests[0], httpx2.Request)
        self.assertIsInstance(raised.exception.response, httpx2.Response)
        self.assertEqual(raised.exception.status_code, 402)

    @unittest.skipUnless(os.environ.get("INCIDENT_AGENT_CLEAN_ENV") == "1", "clean-install check runs in CI/isolated venv")
    def test_clean_install_has_all_pins_and_no_legacy_httpx(self):
        root = Path(__file__).resolve().parents[1]
        for manifest in ("requirements.lock.txt", "requirements-test.lock.txt"):
            for line in (root / manifest).read_text(encoding="utf-8").splitlines():
                if "==" not in line or line.startswith("#"):
                    continue
                pin = line.split(";", 1)[0].strip()
                name, expected = pin.split("==")
                if name == "colorama" and os.name != "nt":
                    continue
                self.assertEqual(metadata.version(name), expected, name)
        self.assertIsNone(util.find_spec("httpx"))
