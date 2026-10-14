"""Configuration-only onboarding and capability isolation; no external I/O."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError
import requests
import tools
from services.configuration import SERVICES
from services.registry import CapabilityUnavailable, ServiceRegistry, registry
from tools.capabilities import get_service_capabilities
from tools.code_changes import get_commit_diff, get_recent_code_changes, get_source_file
from tools.dependencies import check_dependency_health, get_service_dependencies
from tools.health import check_service_health
from tools.logs import query_logs
from tools.metrics import query_metrics
from tools.tool_registry import tool_registry
from tools.version import get_running_version

SHA = "a" * 40
MODULES = (
    "health",
    "logs",
    "metrics",
    "dependencies",
    "code_changes",
    "version",
    "capabilities",
)


def http_service(name="test-service"):
    return {
        "name": name,
        "type": "http_service",
        "health": {
            "provider": "http",
            "base_url": "http://example.test:8123",
            "endpoint": "/ready",
        },
        "logs": {
            "provider": "loki",
            "labels": {
                "service": name,
                "environment": "demo",
            },
        },
        "metrics": {
            "provider": "prometheus",
            "profile": "http_service",
        },
        "dependencies": [],
    }


class RegistryTests(unittest.TestCase):
    def test_order_service_registered(self):
        self.assertEqual(registry.get("order-service").type, "http_service")

    def test_order_db_registered(self):
        self.assertEqual(registry.get("order-db").health.provider, "postgresql")

    def test_inventory_registered(self):
        self.assertTrue(registry.exists("inventory-service"))

    def test_unknown_service(self):
        self.assertIsNone(registry.get("missing"))
        self.assertEqual(
            get_service_capabilities("missing"),
            {
                "status": "no_data",
                "service": "missing",
                "reason": "service_not_registered",
            },
        )

    def test_duplicate_service(self):
        with self.assertRaisesRegex(ValueError, "duplicate service"):
            ServiceRegistry([http_service(), http_service()])

    def test_unknown_dependency(self):
        with self.assertRaisesRegex(ValueError, "unknown dependency"):
            ServiceRegistry([http_service() | {"dependencies": ["missing"]}])

    def test_invalid_health_provider(self):
        entry = http_service()
        entry["health"]["provider"] = "execute_python"
        with self.assertRaises(ValidationError):
            ServiceRegistry([entry])

    def test_invalid_other_providers(self):
        for capability, settings in (
            ("logs", {"labels": {"service": "test"}}),
            ("metrics", {"profile": "http_service"}),
            (
                "source",
                {
                    "repo": "owner/repo",
                    "branch": "main",
                },
            ),
            ("runtime", {"base_url": "http://example.test"}),
        ):
            with self.subTest(capability=capability), self.assertRaises(
                ValidationError
            ):
                ServiceRegistry(
                    [
                        http_service()
                        | {
                            capability: {
                                "provider": "bad",
                                **settings,
                            }
                        }
                    ]
                )

    def test_invalid_metric_profile(self):
        with self.assertRaises(ValidationError):
            ServiceRegistry(
                [
                    http_service()
                    | {
                        "metrics": {
                            "provider": "prometheus",
                            "profile": "unknown",
                        }
                    }
                ]
            )

    def test_invalid_repository(self):
        for repo in ("owner", "https://github.com/a/b", "a/b/c", "../b", "a/.."):
            with self.subTest(repo=repo), self.assertRaises(ValidationError):
                ServiceRegistry(
                    [
                        http_service()
                        | {
                            "source": {
                                "provider": "github",
                                "repo": repo,
                                "branch": "main",
                            }
                        }
                    ]
                )

    def test_http_urls_reject_credentials_and_non_http(self):
        for url in (
            "file:///etc/passwd",
            "localhost:8123",
            "http://user:secret@localhost",
            "http://x:bad",
        ):
            entry = http_service()
            entry["health"]["base_url"] = url
            with self.subTest(url=url), self.assertRaises(ValidationError):
                ServiceRegistry([entry])

    def test_endpoint_rejects_cross_host_and_traversal(self):
        for endpoint in (
            "https://other",
            "//other",
            "/../health",
            "/health?token=secret",
        ):
            entry = http_service()
            entry["health"]["endpoint"] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                ServiceRegistry([entry])

    def test_required_fields(self):
        for key in ("base_url", "provider"):
            entry = http_service()
            del entry["health"][key]
            with self.subTest(key=key), self.assertRaises(ValidationError):
                ServiceRegistry([entry])

    def test_service_key_must_match_name(self):
        with self.assertRaisesRegex(ValueError, "key/name"):
            ServiceRegistry({"different": http_service()})

    def test_self_dependency_rejected(self):
        with self.assertRaisesRegex(ValueError, "self dependency"):
            ServiceRegistry([http_service() | {"dependencies": ["test-service"]}])

    def test_duplicate_dependency_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate dependencies"):
            ServiceRegistry(
                [
                    http_service("parent") | {"dependencies": ["child", "child"]},
                    http_service("child"),
                ]
            )

    def test_capabilities_for_http_services(self):
        for name in ("order-service", "inventory-service"):
            self.assertEqual(
                set(get_service_capabilities(name)["capabilities"]),
                {"health", "logs", "metrics", "dependencies", "runtime", "source"}
                | ({"recovery"} if name == "order-service" else set()),
            )

    def test_database_capabilities(self):
        info = get_service_capabilities("order-db")
        self.assertEqual(
            set(info["capabilities"]), {"health", "metrics", "dependencies"}
        )
        self.assertEqual(info["metric_profile"], "postgresql")

    def test_capability_output_never_serializes_credentials(self):
        with patch.dict(
            os.environ,
            {
                "ORDER_DB_PASSWORD": "secret-db",
                "GITHUB_TOKEN": "secret-token",
                "DEEPSEEK_API_KEY": "secret-model",
            },
        ):
            value = json.dumps(
                [get_service_capabilities(name) for name in registry.names()]
            )
        for sensitive in (
            "secret-db",
            "secret-token",
            "secret-model",
            "password_env",
            "ORDER_DB_PASSWORD",
            "GITHUB_TOKEN",
            "DEEPSEEK_API_KEY",
            "Authorization",
            "base_url",
        ):
            self.assertNotIn(sensitive, value)

    def test_password_literal_forbidden_in_profile(self):
        definitions = deepcopy(SERVICES)
        definitions[1]["health"]["password"] = "secret"
        with self.assertRaises(ValidationError) as caught:
            ServiceRegistry(definitions)
        self.assertNotIn("'secret'", str(caught.exception))

    def test_returned_profile_mutation_cannot_change_registry(self):
        info = registry.get("inventory-service")
        info.logs.labels["service"] = "wrong"
        self.assertEqual(
            registry.get("inventory-service").logs.labels["service"],
            "inventory-service",
        )

    def test_legacy_views_are_derived_and_read_only(self):
        import config

        self.assertEqual(
            config.SERVICE_URLS["inventory-service"],
            registry.get("inventory-service").health.base_url,
        )
        self.assertEqual(
            config.CODE_REPOSITORIES["inventory-service"]["repo"],
            registry.get("inventory-service").source.repo,
        )
        self.assertEqual(
            config.SERVICE_DEPENDENCIES["order-service"],
            [
                {
                    "name": "order-db",
                    "type": "database",
                }
            ],
        )
        self.assertNotIn("password", config.DEPENDENCIES["order-db"])
        with self.assertRaises(TypeError):
            config.SERVICE_URLS["test"] = "http://wrong"

    def test_require_missing_capability(self):
        with self.assertRaises(CapabilityUnavailable) as caught:
            registry.require_capability("order-db", "runtime")
        self.assertEqual(caught.exception.reason, "runtime_capability_not_configured")


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.definitions = deepcopy(SERVICES) + [
            http_service(),
            {
                "name": "minimal",
                "type": "http_service",
            },
        ]
        self.test_registry = ServiceRegistry(self.definitions)
        for module in MODULES:
            self.enterContext(patch(f"tools.{module}.registry", self.test_registry))
        self.http = self.enterContext(
            patch(
                "services.health_providers.ServiceClient.health",
                return_value={
                    "http_status": 200,
                    "body": {
                        "health": "ok",
                        "message": "ready",
                    },
                },
            )
        )
        self.loki = self.enterContext(
            patch(
                "tools.logs.loki_client.query_range",
                return_value={
                    "data": {
                        "result": [
                            {
                                "stream": {"service": "test-service"},
                                "values": [["1000000000", "log"]],
                            }
                        ]
                    }
                },
            )
        )
        self.prom = self.enterContext(
            patch(
                "tools.metrics.prometheus_client.query",
                return_value={
                    "status": "success",
                    "data": {
                        "result": [
                            {"value": [datetime.now(timezone.utc).timestamp(), "1"]},
                        ],
                    },
                },
            )
        )
        self.github = self.enterContext(
            patch(
                "tools.code_changes.github_client.get_recent_commits",
                return_value=[
                    {
                        "sha": SHA,
                        "commit": {
                            "message": "test",
                            "author": {},
                            "committer": {"date": "2026-10-08T00:00:00Z"},
                        },
                    }
                ],
            )
        )

    def test_order_http_health(self):
        self.assertEqual(check_service_health("order-service")["health"], "ok")

    def test_inventory_http_health(self):
        self.assertEqual(check_service_health("inventory-service")["http_status"], 200)

    def test_missing_health(self):
        self.assertEqual(
            check_service_health("minimal")["reason"],
            "health_capability_not_configured",
        )
        self.http.assert_not_called()

    def test_postgres_health_stages(self):
        stages = dict(
            dns_resolved=True,
            tcp_reachable=True,
            database_connected=True,
            query_ok=True,
        )
        with patch.dict(os.environ, {"ORDER_DB_PASSWORD": "secret"}), patch(
            "services.health_providers.DatabaseClient.health", return_value=stages
        ):
            result = check_service_health("order-db")
            self.assertTrue(result["reachable"])
            for key, value in stages.items():
                self.assertEqual(result[key], value)

    def test_postgres_missing_credentials_is_no_data(self):
        with patch.dict(
            os.environ,
            {},
            clear=True,
        ), patch("services.health_providers.DatabaseClient") as client:
            self.assertEqual(
                check_dependency_health("order-db")["reason"],
                "database_credentials_not_configured",
            )
        client.assert_not_called()

    def test_postgres_failure_message_cannot_leak_password(self):
        with patch.dict(os.environ, {"ORDER_DB_PASSWORD": "secret"}), patch(
            "services.health_providers.DatabaseClient.health",
            return_value={
                "dns_resolved": True,
                "tcp_reachable": True,
                "database_connected": False,
                "query_ok": False,
                "failure_stage": "database",
                "error_type": "OperationalError",
                "message": "password=secret",
            },
        ):
            result = check_dependency_health("order-db")
        self.assertFalse(result["reachable"])
        self.assertEqual(result["failure_stage"], "database")
        self.assertNotIn("secret", json.dumps(result))

    def test_configured_endpoint_is_used(self):
        self.assertEqual(check_service_health("test-service")["health"], "ok")
        self.http.assert_called_once_with(endpoint="/ready")

    def test_http_transport_degrades(self):
        self.http.side_effect = requests.Timeout("secret")
        result = check_service_health("test-service")
        self.assertEqual(result["status"], "no_data")
        self.assertNotIn("secret", json.dumps(result))

    def test_http_non_object_response_degrades(self):
        self.http.return_value["body"] = []
        self.assertEqual(
            check_service_health("test-service")["reason"], "invalid_health_response"
        )

    def test_order_loki_selector(self):
        query_logs("order-service")
        self.assertEqual(
            self.loki.call_args.kwargs["logql"], '{service="order-service"}'
        )

    def test_inventory_loki_selector(self):
        query_logs("inventory-service")
        self.assertEqual(
            self.loki.call_args.kwargs["logql"], '{service="inventory-service"}'
        )

    def test_multiple_loki_labels(self):
        query_logs("test-service")
        self.assertEqual(
            self.loki.call_args.kwargs["logql"],
            '{service="test-service",environment="demo"}',
        )

    def test_loki_value_escaping(self):
        definitions = deepcopy(self.definitions)
        definitions[-2]["logs"]["labels"]["environment"] = 'quote"slash\\line\n'
        with patch("tools.logs.registry", ServiceRegistry(definitions)):
            query_logs("test-service")
        self.assertEqual(
            self.loki.call_args.kwargs["logql"],
            '{service="test-service",environment='
            + json.dumps('quote"slash\\line\n')
            + "}",
        )

    def test_invalid_label_name_rejected(self):
        entry = http_service()
        entry["logs"]["labels"] = {'service=~".*"': "x"}
        with self.assertRaises(ValidationError):
            ServiceRegistry([entry])

    def test_missing_logs_no_request(self):
        self.assertEqual(
            query_logs("minimal")["reason"], "logs_capability_not_configured"
        )
        self.loki.assert_not_called()

    def test_model_cannot_supply_logql(self):
        with self.assertRaises(TypeError):
            query_logs("test-service", logql='{arbitrary="x"}')
        query_logs('{service=~".*"}')
        self.loki.assert_not_called()

    def test_http_services_share_promql_templates(self):
        for metric in ("http_requests_total", "http_request_rate", "http_5xx_rate"):
            first = query_metrics("order-service", metric)["promql"]
            second = query_metrics("inventory-service", metric)["promql"]
            self.assertEqual(
                first.replace("order-service", "inventory-service"), second
            )

    def test_postgres_profile(self):
        result = query_metrics("order-db", "postgres_connections")
        self.assertEqual(
            result["promql"],
            'sum(pg_stat_database_numbackends{job="postgres",datname="orders"})',
        )

    def test_http_rejects_postgres_metric(self):
        self.assertEqual(
            query_metrics("inventory-service", "postgres_up")["reason"],
            "metric_not_supported_by_profile",
        )
        self.prom.assert_not_called()

    def test_postgres_rejects_http_metric(self):
        self.assertEqual(
            query_metrics("order-db", "http_5xx_rate")["reason"],
            "metric_not_supported_by_profile",
        )
        self.prom.assert_not_called()

    def test_missing_metrics_no_request(self):
        self.assertEqual(
            query_metrics("minimal", "http_5xx_rate")["reason"],
            "metrics_capability_not_configured",
        )
        self.prom.assert_not_called()

    def test_metric_labels_configured(self):
        definitions = deepcopy(self.definitions)
        definitions[-2]["metrics"]["labels"] = {
            "app": "stock",
            "env": "demo",
        }
        with patch("tools.metrics.registry", ServiceRegistry(definitions)):
            result = query_metrics("test-service", "http_request_rate")
        self.assertEqual(
            result["promql"],
            'sum(rate(demo_http_requests_total{app="stock",env="demo"}[1m]))',
        )

    def test_metrics_transport_error_does_not_leak(self):
        self.prom.side_effect = requests.Timeout("sensitive connection details")
        result = query_metrics("test-service", "http_request_rate")
        self.assertEqual(result["status"], "no_data")
        self.assertNotIn("sensitive", json.dumps(result))

    def test_invalid_metrics_response(self):
        for response in (
            [],
            {"data": []},
            {"data": {"result": {}}},
            {"data": {"result": [{}]}},
        ):
            self.prom.return_value = response
            self.assertEqual(
                query_metrics("test-service", "http_request_rate")["reason"],
                "invalid_metrics_response",
            )

    def test_logs_transport_error_does_not_leak(self):
        self.loki.side_effect = requests.Timeout("sensitive connection details")
        result = query_logs("test-service")
        self.assertEqual(result["status"], "no_data")
        self.assertNotIn("sensitive", json.dumps(result))

    def test_invalid_logs_response(self):
        for response in ([], {"data": []}, {"data": {"result": {}}}):
            self.loki.return_value = response
            self.assertEqual(
                query_logs("test-service")["reason"], "invalid_logs_response"
            )

    def test_source_services_share_configured_repository(self):
        for name in ("order-service", "inventory-service"):
            self.assertEqual(
                get_recent_code_changes(name)["repository"], "yuj1ng1/prometheus-demo"
            )
        self.assertEqual(self.github.call_count, 2)

    def test_missing_source_all_tools_no_data(self):
        for result in (
            get_recent_code_changes("minimal"),
            get_commit_diff("minimal", SHA),
            get_source_file(
                "minimal",
                "app.py",
                SHA,
            ),
        ):
            self.assertEqual(result["reason"], "source_capability_not_configured")
        self.github.assert_not_called()

    def test_arbitrary_repo_rejected(self):
        with self.assertRaises(TypeError):
            get_recent_code_changes("inventory-service", repo="other/repo")

    def version(self, name, flag):
        return {
            "http_status": 200,
            "body": {
                "service": name,
                "commit_sha": SHA,
                "process_started_at": "2026-10-08T00:00:00Z",
                "runtime_flags": {
                    flag: True,
                    "simulate_slow_request": False,
                    "GITHUB_TOKEN": "secret",
                },
            },
        }

    def test_order_runtime(self):
        with patch(
            "tools.version.ServiceClient.version",
            return_value=self.version("order-service", "simulate_code_regression"),
        ):
            self.assertEqual(get_running_version("order-service")["status"], "ok")

    def test_inventory_runtime_filters_flags(self):
        response = self.version("inventory-service", "simulate_inventory_error")
        response["body"]["runtime_flags"]["simulate_code_regression"] = True
        with patch("tools.version.ServiceClient.version", return_value=response):
            self.assertEqual(
                get_running_version("inventory-service")["runtime_flags"],
                {
                    "simulate_inventory_error": True,
                    "simulate_slow_request": False,
                },
            )

    def test_missing_runtime_no_request(self):
        with patch("tools.version.ServiceClient.version") as client:
            self.assertEqual(
                get_running_version("minimal")["reason"],
                "runtime_capability_not_configured",
            )
        client.assert_not_called()

    def test_runtime_service_must_match(self):
        with patch(
            "tools.version.ServiceClient.version",
            return_value=self.version("order-service", "simulate_inventory_error"),
        ):
            self.assertEqual(
                get_running_version("inventory-service")["reason"],
                "version_service_identity_unconfirmed",
            )

    def test_custom_runtime_endpoint(self):
        definitions = deepcopy(self.definitions)
        definitions[-2]["runtime"] = {
            "provider": "http_version",
            "base_url": "http://example.test",
            "endpoint": "/build",
        }
        with patch("tools.version.registry", ServiceRegistry(definitions)), patch(
            "tools.version.ServiceClient.version",
            return_value=self.version("test-service", "simulate_any"),
        ) as client:
            self.assertEqual(get_running_version("test-service")["runtime_flags"], {})
        client.assert_called_once_with(endpoint="/build")

    def test_order_dependencies(self):
        self.assertEqual(
            get_service_dependencies("order-service")["dependencies"],
            [
                {
                    "name": "order-db",
                    "type": "database",
                }
            ],
        )

    def test_inventory_dependencies_empty(self):
        self.assertEqual(
            get_service_dependencies("inventory-service")["dependencies"], []
        )

    def test_database_dependencies_empty(self):
        self.assertEqual(get_service_dependencies("order-db")["dependencies"], [])

    def test_dependency_health_http_provider(self):
        self.assertTrue(check_dependency_health("test-service")["reachable"])

    def test_third_service_onboards_through_configuration_only(self):
        # The same unmodified five Tools accept a name appearing only in this test's config.
        results = (
            check_service_health("test-service"),
            query_logs("test-service"),
            query_metrics("test-service", "http_request_rate"),
            get_service_dependencies("test-service"),
            get_service_capabilities("test-service"),
        )
        for result in results:
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["service"], "test-service")
        self.assertEqual(results[3]["dependencies"], [])
        self.assertEqual(
            set(results[4]["capabilities"]),
            {"health", "logs", "metrics", "dependencies"},
        )
        self.assertEqual(results[4]["missing_capabilities"], ["runtime", "source"])


class ToolBoundaryTests(unittest.TestCase):
    def test_capability_tool_registered(self):
        self.assertEqual(
            tool_registry["get_service_capabilities"]["schema"]["parameters"][
                "required"
            ],
            ["service"],
        )

    def test_all_original_tools_still_registered(self):
        expected = {
            "query_logs",
            "query_metrics",
            "check_scrape_target",
            "query_metric_range",
            "query_metrics_aligned",
            "check_service_health",
            "get_service_dependencies",
            "check_dependency_health",
            "get_recent_code_changes",
            "get_commit_diff",
            "get_source_file",
            "get_running_version",
            "get_service_capabilities",
        }
        self.assertEqual(set(tool_registry), expected)

    def test_providers_and_actions_not_agent_tools(self):
        for name in (
            "probe_health",
            "selector",
            "build_promql",
            "registry",
            "disable_demo_regression",
            "approve_proposal",
            "execute",
            "generic_restart_service",
        ):
            self.assertNotIn(name, tool_registry)

    def test_remediation_declaration_does_not_expand_executor(self):
        from remediation.executor import ACTION_REGISTRY

        self.assertEqual(set(ACTION_REGISTRY), {"disable_demo_regression"})
        self.assertEqual(
            get_service_capabilities("inventory-service")["remediation"], []
        )
        self.assertEqual(
            get_service_capabilities("order-service")["remediation"],
            ["disable_demo_regression"],
        )

    def test_prompt_selects_capabilities_before_demo_examples(self):
        from prompts.agent_prompt import agent_prompt

        self.assertIn("get_service_capabilities(service)", agent_prompt)
        self.assertIn("dependencies=[]", agent_prompt)
        self.assertIn("不能据此排除现实中尚未登记的依赖", agent_prompt)
        self.assertIn("证据缺口", agent_prompt)


if __name__ == "__main__":
    unittest.main()
