"""Code evidence contracts; GitHub, service and Loki HTTP responses are isolated."""
import base64
import json
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
import tools
from clients.github_client import GithubClient
from clients.service_client import ServiceClient
from clients.loki_client import LokiClient
from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from tools import code_changes as code
from tools.logs import query_logs, timestamp_to_utc
from tools.version import get_running_version
from tools.tool_registry import tool_registry

SHA = "a" * 40
REPO = "yuj1ng1/prometheus-demo"


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def commit(files=None):
    return {"sha": SHA, "commit": {"message": "change", "committer": {
        "date": "2026-10-07T07:16:13Z"}}, "files": files or [changed_file()]}


def changed_file(name="app.py", patch_text="+raise RuntimeError('regression')"):
    return {"filename": name, "status": "modified", "additions": 1,
            "deletions": 0, "changes": 1, "patch": patch_text}


def source(data=b"print('hello')\n", path="app.py"):
    return {"type": "file", "path": path, "size": len(data), "sha": "b" * 40,
            "encoding": "base64", "content": base64.b64encode(data).decode()}


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.body = {
            "service": "order-service",
            "commit_sha": SHA,
            "short_sha": SHA[:7],
            "process_started_at": "2026-10-07T08:21:30Z",
            "runtime_flags": {
                "simulate_code_regression": True,
                "simulate_slow_request": False,
            },
        }
        self.version_patch = patch("tools.version.ServiceClient.version", return_value={
            "http_status": 200, "body": self.body})
        self.mock = self.version_patch.start()
        self.addCleanup(patch.stopall)

    def test_normal_version(self):
        result = get_running_version("order-service")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["commit_sha"], SHA)
        self.assertEqual(result["short_sha"], SHA[:7])

    def test_unknown_service_does_not_request(self):
        self.assertEqual(get_running_version("other")["status"], "no_data")
        self.mock.assert_not_called()

    def test_unknown_commit_preserves_observed_flags_and_start(self):
        for value in (None, "", "unknown"):
            self.body["commit_sha"] = value
            result = get_running_version("order-service")
            self.assertEqual(result["reason"], "commit_unknown")
            self.assertIsNone(result["commit_sha"])
            self.assertTrue(result["runtime_flags"]["simulate_code_regression"])

    def test_only_allowed_runtime_flags_are_returned(self):
        self.body["runtime_flags"]["GITHUB_TOKEN"] = "must not escape"
        self.body["environment"] = {"password": "must not escape"}
        result = get_running_version("order-service")
        self.assertEqual(
            result["runtime_flags"],
            {
                "simulate_code_regression": True,
                "simulate_slow_request": False,
            },
        )
        self.assertNotIn("must not escape", json.dumps(result))
        self.body["runtime_flags"]["simulate_code_regression"] = False
        self.assertFalse(get_running_version("order-service")["runtime_flags"]["simulate_code_regression"])

    def test_utc_time_is_preserved(self):
        for value in ("2026-10-07T08:21:30Z", "2026-10-07T08:21:30.123+00:00"):
            self.body["process_started_at"] = value
            self.assertEqual(get_running_version("order-service")["process_started_at"], value)

    def test_naive_and_non_utc_time_are_not_deployment_evidence(self):
        for value in ("2026-10-07 16:21:30", "2026-10-07T16:21:30+08:00", None):
            self.body["process_started_at"] = value
            self.assertEqual(get_running_version("order-service")["reason"], "invalid_process_started_at")

    def test_invalid_response_and_sha(self):
        for body in ([], {}, {**self.body, "service": "other"},
                     {**self.body, "commit_sha": "4bb6348"},
                     {**self.body, "runtime_flags": {"simulate_code_regression": "true"}}):
            self.mock.return_value = {"http_status": 200, "body": body}
            self.assertEqual(get_running_version("order-service")["status"], "no_data")

    def test_missing_route_and_transport_error(self):
        self.mock.return_value = {"http_status": 404, "body": None}
        self.assertEqual(get_running_version("order-service")["http_status"], 404)
        self.mock.side_effect = requests.Timeout("sensitive transport detail")
        result = get_running_version("order-service")
        self.assertEqual(result["reason"], "version_unreachable")
        self.assertNotIn("sensitive", json.dumps(result))
        self.mock.side_effect = ValueError()
        self.assertEqual(get_running_version("order-service")["reason"], "invalid_version_response")

    def test_service_client_requests_version_route(self):
        self.version_patch.stop()
        response = Mock(status_code=200)
        response.json.return_value = self.body
        with patch("clients.service_client.requests.get", return_value=response) as get:
            self.assertEqual(ServiceClient("http://service/").version()["body"], self.body)
        get.assert_called_once_with("http://service/version", timeout=5)


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.mock = patch.object(code.github_client, "get_commit", return_value=commit()).start()
        self.addCleanup(patch.stopall)

    def test_normal_commit(self):
        result = code.get_commit_diff("order-service", SHA)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["repository"], REPO)
        self.assertEqual(result["commit"]["committed_at"], "2026-10-07T07:16:13Z")
        self.assertFalse(result["truncated"])
        self.mock.assert_called_once_with(REPO, SHA, file_limit=15)

    def test_unconfigured_service_and_arbitrary_repo_are_rejected(self):
        for value in ("missing", "attacker/arbitrary-repo", "https://github.com/other/repo"):
            self.assertEqual(code.get_commit_diff(value, SHA)["status"], "no_data")
        self.mock.assert_not_called()
        with self.assertRaises(TypeError):
            code.get_commit_diff(service="order-service", commit_sha=SHA, repo="other/repo")

    def test_missing_commit(self):
        self.mock.side_effect = http_error(404)
        self.assertEqual(code.get_commit_diff("order-service", SHA)["reason"], "not_found")

    def test_multiple_files(self):
        self.mock.return_value = commit([changed_file(), changed_file("lib.py")])
        self.assertEqual(len(code.get_commit_diff("order-service", SHA)["files"]), 2)

    def test_per_patch_truncation(self):
        self.mock.return_value = commit([changed_file(patch_text="x" * 6001)])
        result = code.get_commit_diff("order-service", SHA)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["files"][0]["truncated"])
        self.assertEqual(len(result["files"][0]["patch"]), 6000)

    def test_total_patch_and_file_limits(self):
        self.mock.return_value = commit([changed_file(f"{i}.py", "x" * 6000) for i in range(25)])
        result = code.get_commit_diff("order-service", SHA)
        self.assertEqual(len(result["files"]), 15)
        self.assertEqual(sum(len(f["patch"]) for f in result["files"]), 24000)
        self.assertTrue(result["files_truncated"])

    def test_api_pagination_and_missing_patch_are_explicit(self):
        self.mock.return_value = {**commit(), "_files_paginated": True}
        self.assertTrue(code.get_commit_diff("order-service", SHA)["files_truncated"])
        self.mock.return_value = commit([{k: v for k, v in changed_file().items() if k != "patch"}])
        result = code.get_commit_diff("order-service", SHA)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["files"][0]["patch_available"])
        self.assertIsNone(result["files"][0]["patch"])

    def test_bad_sha_and_mismatched_api_response(self):
        for sha in ("main", "../abc", "a" * 41, "abcdef", None):
            self.assertEqual(code.get_commit_diff("order-service", sha)["reason"], "invalid_commit_sha")
        self.mock.assert_not_called()
        self.mock.return_value = {**commit(), "sha": "b" * 40}
        self.assertEqual(code.get_commit_diff("order-service", SHA)["reason"], "invalid_github_response")

    def test_permission_network_and_invalid_json_errors(self):
        for exc, reason in ((http_error(403), "access_denied_or_rate_limited"),
                            (requests.Timeout(), "github_unreachable"),
                            (ValueError(), "invalid_github_response")):
            self.mock.side_effect = exc
            self.assertEqual(code.get_commit_diff("order-service", SHA)["reason"], reason)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.mock = patch.object(code.github_client, "get_file_content", return_value=source()).start()
        self.addCleanup(patch.stopall)

    def test_normal_text_and_exact_ref(self):
        result = code.get_source_file("order-service", "app.py", SHA)
        self.assertEqual(result["content"], "print('hello')\n")
        self.assertEqual(result["ref"], SHA)
        self.assertFalse(result["truncated"])
        self.mock.assert_called_once_with(REPO, "app.py", SHA)

    def test_branch_ref_is_preserved(self):
        result = code.get_source_file("order-service", "app.py", "codex/postgres-monitoring")
        self.assertEqual(result["ref"], "codex/postgres-monitoring")

    def test_missing_file(self):
        self.mock.side_effect = http_error(404)
        self.assertEqual(code.get_source_file("order-service", "app.py", SHA)["reason"], "not_found")

    def test_binary_and_unsupported_encoding(self):
        for data in (b"\x00PNG", b"\xff\xfe\x81", b"print\x01"):
            self.mock.return_value = source(data)
            self.assertEqual(code.get_source_file("order-service", "app.py", SHA)["reason"], "binary_or_unsupported")
        self.mock.return_value = {**source(), "encoding": "unknown"}
        self.assertEqual(code.get_source_file("order-service", "app.py", SHA)["reason"], "unsupported_encoding")

    def test_large_text_is_explicitly_truncated(self):
        self.mock.return_value = source(("中" * 20001).encode())
        result = code.get_source_file("order-service", "app.py", SHA)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), 20000)

    def test_raw_prefix_for_files_over_one_mb(self):
        self.mock.return_value = {**source(), "encoding": "none", "content": "", "size": 2_000_000}
        prefix = b"x" * 80000 + "中".encode()[:2]
        with patch.object(code.github_client, "get_file_prefix", return_value=prefix) as raw:
            result = code.get_source_file("order-service", "app.py", SHA)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["truncated"])
        raw.assert_called_once_with(REPO, "app.py", SHA, code.MAX_SOURCE_BYTES)

    def test_illegal_path_does_not_request(self):
        for path in ("../app.py", "a/../app.py", "/app.py", "C:/app.py", "a\\app.py",
                     "%2e%2e/app.py", "app.py?ref=main", "a//b", "a/./b", "", "a\x00b"):
            self.assertEqual(code.get_source_file("order-service", path, SHA)["reason"], "invalid_path")
        self.mock.assert_not_called()

    def test_directory_symlink_invalid_json_and_repo(self):
        for body in ([], {"type": "dir"}, {"type": "symlink"}, {**source(), "path": "other.py"}):
            self.mock.return_value = body
            self.assertEqual(code.get_source_file("order-service", "app.py", SHA)["status"], "no_data")
        self.assertEqual(code.get_source_file("other/repo", "app.py", SHA)["status"], "no_data")

    def test_invalid_ref_does_not_request(self):
        for ref in ("", "../main", "main?x=1", "main\n", None):
            self.assertEqual(code.get_source_file("order-service", "app.py", ref)["reason"], "invalid_ref")
        self.mock.assert_not_called()

    def test_incomplete_base64_response_is_not_complete_source_evidence(self):
        self.mock.return_value = {**source(), "size": 1000}
        self.assertEqual(code.get_source_file("order-service", "app.py", SHA)["reason"],
                         "invalid_github_response")


class ClientAndLogTests(unittest.TestCase):
    def test_github_routes_are_read_only_and_refs_are_params(self):
        response = Mock(links={"next": {"url": "next"}})
        response.json.return_value = commit()
        client = GithubClient()
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-only"}):
            with patch("clients.github_client.requests.get", return_value=response) as get:
                self.assertTrue(client.get_commit(REPO, SHA)["_files_paginated"])
                self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-only")
                client.get_file_content(REPO, "src/my file.py", "codex/postgres-monitoring")
                self.assertIn("contents/src/my%20file.py", get.call_args.args[0])
                self.assertEqual(get.call_args.kwargs["params"], {"ref": "codex/postgres-monitoring"})

    def test_raw_download_stops_at_byte_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = iter([b"a" * 8192, b"b" * 8192])
        with patch("clients.github_client.requests.get", return_value=response) as get:
            result = GithubClient().get_file_prefix(REPO, "app.py", SHA, 10000)
        self.assertEqual(len(result), 10000)
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_nanoseconds_convert_to_exact_utc_and_original_fields_survive(self):
        seconds = int(datetime(2026, 10, 7, 8, 20, 34, tzinfo=timezone.utc).timestamp())
        timestamp = str(seconds * 1_000_000_000 + 123456789)
        line = "2026-10-07 16:20:34 RuntimeError"
        response = {"data": {"result": [{"stream": {"service": "order-service"},
                                          "values": [[timestamp, line]]}]}}
        with patch("tools.logs.loki_client.query_range", return_value=response):
            result = query_logs("order-service")["logs"][0]
        self.assertEqual(result["timestamp_utc"], "2026-10-07T08:20:34.123456789Z")
        self.assertEqual(result["timestamp"], timestamp)
        self.assertEqual(result["line"], line)
        self.assertEqual(result["labels"], {"service": "order-service"})

    def test_utc_seconds_and_cross_day_boundary(self):
        self.assertEqual(timestamp_to_utc("0"), "1970-01-01T00:00:00Z")
        self.assertEqual(timestamp_to_utc("86400000000000"), "1970-01-02T00:00:00Z")

    def test_logs_are_sorted_by_original_nanoseconds(self):
        response = {"data": {"result": [{"values": [["1000000001", "older"], ["1000000002", "newer"]]}]}}
        with patch("tools.logs.loki_client.query_range", return_value=response):
            self.assertEqual(query_logs("order-service")["logs"][0]["line"], "newer")

    def test_loki_uses_correct_direction_parameter(self):
        with patch("clients.loki_client.requests.get") as get:
            LokiClient("http://loki").query_range("{service=\"order-service\"}")
        self.assertEqual(get.call_args.kwargs["params"]["direction"], "backward")
        self.assertNotIn("diection", get.call_args.kwargs["params"])

    def test_recent_commits_use_committer_time_not_author_time(self):
        body = [{"sha": SHA, "commit": {"message": "change", "author": {
            "name": "author", "date": "2020-01-01T00:00:00Z"}, "committer": {
            "date": "2026-10-07T07:16:13Z"}}}]
        with patch.object(code.github_client, "get_recent_commits", return_value=body):
            result = code.get_recent_code_changes("order-service")
        self.assertEqual(result["changes"][0]["committed_at"], "2026-10-07T07:16:13Z")

    def test_new_tools_registered_and_helpers_not_registered(self):
        expected = {"get_running_version": ["service"], "get_commit_diff": ["service", "commit_sha"],
                    "get_source_file": ["service", "path", "ref"]}
        for name, args in expected.items():
            self.assertEqual(tool_registry[name]["schema"]["parameters"]["required"], args)
            self.assertNotIn("repo", tool_registry[name]["schema"]["parameters"]["properties"])
        for name in ("repository_for", "read_error", "valid_path", "valid_ref", "timestamp_to_utc"):
            self.assertNotIn(name, tool_registry)

    def test_local_budget_and_remaining_instructions_are_retained(self):
        from agent.agent import Agent
        from models.incident import Incident, Alert
        self.assertEqual(MAX_TOOL_CALLS, 13)
        self.assertEqual(MAX_AGENT_LOOPS, 7)
        incident = Incident(incident_id="test-budget", service="order-service", severity="critical",
                            status="investigating", alerts=[Alert(service="order-service",
                            metric="http_5xx_rate", value=1, threshold=.2, severity="critical")])
        call = SimpleNamespace(type="function_call", name="get_running_version", call_id="one",
                               arguments='{"service":"order-service"}')
        model = Mock()
        model.call_model.side_effect = [SimpleNamespace(status="completed", output=[call]),
                                       SimpleNamespace(status="completed", output=[], output_text="done")]
        agent = Agent(Mock(), "investigate")
        agent.model_client = model
        with patch.object(agent.tool_manager, "run_tool", return_value=(
                True, {"type":"function_call_output", "call_id":"one", "output":"{}"},
                {"status":"ok", "commit_sha":SHA})):
            self.assertTrue(agent.investigate(incident).success)
        instructions = [item.kwargs["instructions"] for item in model.call_model.call_args_list]
        self.assertIn("剩余工具调用次数：13", instructions[0])
        self.assertIn("剩余工具调用次数：12", instructions[1])
        self.assertIn("剩余模型响应轮数（含本轮）：6", instructions[1])

    def test_local_budget_is_retained(self):
        self.assertEqual(MAX_TOOL_CALLS, 13)
        self.assertEqual(MAX_AGENT_LOOPS, 7)


if __name__ == "__main__":
    unittest.main()
