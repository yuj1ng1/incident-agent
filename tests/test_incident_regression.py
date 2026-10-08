"""Regression checks for the current code; external responses and data are isolated.

Run with: python -B -m unittest discover -s tests -v
No production Incident data or production log is written by these tests.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
import runpy
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
LOG_TEMP = tempfile.TemporaryDirectory(prefix="incident-agent-regression-log-")
REAL_FILE_HANDLER = logging.FileHandler


def isolated_log_handler(filename, *args, **kwargs):
    if Path(filename).resolve() == ROOT / "agent_logger" / "agent.log":
        filename = Path(LOG_TEMP.name) / "agent.log"
    return REAL_FILE_HANDLER(filename, *args, **kwargs)


with patch("logging.FileHandler", side_effect=isolated_log_handler):
    import tools
    from agent.agent import Agent
    from alert_parser import parse_prometheus_alert
    from clients.prometheus_client import PrometheusClient
    from config import (
        HTTP_5XX_RATE_INCREASE_THRESHOLD,
        MAX_AGENT_LOOPS,
        MODEL_NAME,
        REINVESTIGATE_COOLDOWN_MINUTES,
    )
    from fingerprint import generate_alert_fingerprint
    from incident_factory import create_incident_from_alert
    from incident_store import IncidentStore
    from models.incident import Alert, Incident
    from models.investigation import InvestigationStatus
    from monitor.incident_monitor import IncidentMonitor
    from openai.types.responses import Response
    from prompts.agent_prompt import agent_prompt
    from tools.metrics import query_metrics
    from tools.tool_registry import tool_registry
    import reinvestigation


NOW = datetime(2026, 10, 6, 14, 30, 0, tzinfo=timezone(timedelta(hours=8)))


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def raw_alert(value=0.27, service="order-service", metric="http_5xx_rate", state="firing"):
    return {
        "state": state,
        "labels": {
            "alertname": "HighHttp5xxRate",
            "service": service,
            "metric": metric,
            "signal_type": "numeric",
            "severity": "critical",
        },
        "annotations": {
            "threshold": "0.20",
            "unit": "ratio",
            "direction": "higher_is_worse",
            "window": "1m",
            "minimum_requests": "20",
        },
        "activeAt": "2026-10-06T06:30:00Z",
        "value": str(value),
    }


def tool_call(call_id="call_1", name="query_metrics", arguments=None):
    arguments = arguments if arguments is not None else {
        "service": "order-service", "metric": "http_5xx_rate"
    }
    return {
        "type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
        "name": name, "arguments": json.dumps(arguments),
    }


def model_response(answer="Test investigation completed.", calls=(), status="completed", reasoning=False):
    output = list(calls)
    if reasoning:
        output.insert(0, {"type": "reasoning", "id": "rs_test", "summary": []})
    if answer:
        output.append({
            "type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": answer, "annotations": []}],
        })
    return Response.model_validate({
        "id": "resp_test", "created_at": 0, "model": MODEL_NAME, "object": "response",
        "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
    })


def tearDownModule():
    logger = logging.getLogger("agent")
    for handler in list(logger.handlers):
        if isinstance(handler, REAL_FILE_HANDLER) and Path(handler.baseFilename).parent == Path(LOG_TEMP.name):
            logger.removeHandler(handler)
            handler.close()
    LOG_TEMP.cleanup()


class IncidentRegressionTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="incident-agent-regression-data-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.store = IncidentStore(self.work / "data" / "incidents.json")
        self.sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=model_response())))
        self.agent = Agent(self.sdk, agent_prompt)
        self.prometheus = PrometheusClient("http://localhost:9090")
        self.prometheus.get_alerts = Mock()
        self.monitor = IncidentMonitor(self.prometheus, self.agent, self.store, poll_interval=0)
        self.fp = generate_alert_fingerprint(parse_prometheus_alert(raw_alert()))
        self.prometheus.get_rules = Mock(
            return_value={
                "status": "success",
                "data": {
                    "groups": [
                        {
                            "rules": [
                                {
                                    "name": "HighHttp5xxRate",
                                    "type": "alerting",
                                    "health": "ok",
                                    "lastEvaluation": NOW.isoformat(),
                                    "annotations": raw_alert()["annotations"],
                                },
                            ],
                        },
                    ],
                },
            },
        )
        self.prometheus.query = Mock(
            side_effect=lambda expression: {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "value": [
                                NOW.timestamp(),
                                "1" if expression.startswith("min(up{") else "0",
                            ],
                        },
                    ],
                },
            },
        )
        self.enterContext(patch("alert_recovery.datetime", FixedDatetime))
        self.enterContext(patch("monitor.incident_monitor.datetime", FixedDatetime))
        self.enterContext(patch("reinvestigation.datetime", FixedDatetime))

    def poll(self, *alerts):
        self.prometheus.get_alerts.return_value = {"status": "success", "data": {"alerts": list(alerts)}}
        self.monitor.process_alerts_once()

    def seed(self, value=0.27, last_at=NOW, last_value=None):
        incident = create_incident_from_alert(
            parse_prometheus_alert(raw_alert(max(value, 0.21))).model_copy(update={"value": value}), self.fp
        )
        incident.last_investigated_at = last_at
        incident.last_investigated_value = value if last_value is None else last_value
        self.store.save_incident(self.fp, incident)
        return incident

    def investigate_with_metrics(self, incident, value=0.27, empty=False):
        self.sdk.responses.create.side_effect = [
            model_response(answer=None, calls=[tool_call()], reasoning=True),
            model_response(),
        ]
        result = [] if empty else [{"metric": {}, "value": [0, str(value)]}]
        with patch("tools.metrics.prometheus_client.query", return_value={
            "status": "success", "data": {"resultType": "vector", "result": result}
        }) as query:
            investigated = self.agent.investigate(incident)
        return investigated, query

    def test_main_constructs_real_objects_and_enters_two_poll_cycles(self):
        old_cwd = Path.cwd()
        try:
            os.chdir(self.work)
            with patch.object(PrometheusClient, "get_alerts", return_value={
                "status": "success", "data": {"alerts": []}
            }) as get_alerts, patch("monitor.incident_monitor.time.sleep", side_effect=[None, KeyboardInterrupt]), \
                    patch.dict(os.environ, {"DEEPSEEK_API_KEY": "regression-test-placeholder"}):
                namespace = runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
            self.assertIsInstance(namespace["agent"], Agent)
            self.assertIsInstance(namespace["prometheus_client"], PrometheusClient)
            self.assertIsInstance(namespace["incident_store"], IncidentStore)
            self.assertIsInstance(namespace["monitor"], IncidentMonitor)
            self.assertEqual(get_alerts.call_count, 2)
            self.assertEqual(json.loads((self.work / "data" / "incidents.json").read_text()), {
                "version": 2, "incidents": {}, "active_by_fingerprint": {}
            })
        finally:
            os.chdir(old_cwd)

    def test_firing_alert_parses_into_typed_alert(self):
        alert = parse_prometheus_alert(raw_alert())
        self.assertIsInstance(alert, Alert)
        self.assertEqual((alert.service, alert.metric, alert.value, alert.threshold, alert.severity),
                         ("order-service", "http_5xx_rate", 0.27, 0.2, "critical"))

    def test_pending_and_inactive_alerts_are_ignored(self):
        for state in ("pending", "inactive", None):
            with self.subTest(state=state):
                self.assertIsNone(parse_prometheus_alert(raw_alert(state=state)))
        self.poll(raw_alert(state="pending"))
        self.assertEqual(self.store.load(), {
            "version": 2, "incidents": {}, "active_by_fingerprint": {}
        })
        self.sdk.responses.create.assert_not_called()

    def test_fingerprint_is_stable_across_value_and_severity_changes(self):
        changed = raw_alert(value=0.8)
        changed["labels"]["severity"] = "warning"
        changed["annotations"]["threshold"] = "0.10"
        self.assertEqual(generate_alert_fingerprint(parse_prometheus_alert(changed)), self.fp)
        self.assertNotEqual(self.fp, hashlib.sha256(b"order-service|http_5xx_rate").hexdigest())

    def test_fingerprint_separates_different_services_and_metrics(self):
        self.assertNotEqual(generate_alert_fingerprint(parse_prometheus_alert(raw_alert(service="inventory-service"))), self.fp)
        self.assertNotEqual(generate_alert_fingerprint(parse_prometheus_alert(raw_alert()).model_copy(update={"alertname": "HighHttpLatency"})), self.fp)

    def test_new_incident_is_saved_before_real_agent_starts(self):
        seen = []

        def model_call(**kwargs):
            incident = self.store.get_active_incident(self.fp)
            self.assertIsNotNone(incident)
            seen.append(incident.incident_id)
            self.assertEqual(incident.status, "investigating")
            self.assertEqual(incident.alerts[0].value, 0.27)
            self.assertEqual(incident.fingerprint, self.fp)
            self.assertEqual({t["name"] for t in kwargs["tools"]},
                             set(tool_registry))
            return model_response()

        self.sdk.responses.create.side_effect = model_call
        self.poll(raw_alert())
        incident = self.store.get_active_incident(self.fp)
        self.assertEqual(seen, [incident.incident_id])
        self.assertRegex(incident.incident_id, r"^inc-[0-9a-f]{8}$")
        self.assertEqual(incident.investigations[0].status, InvestigationStatus.COMPLETED)

    def test_new_incident_tool_results_and_investigation_persist(self):
        self.sdk.responses.create.side_effect = [
            model_response(answer=None, calls=[tool_call()], reasoning=True), model_response()
        ]
        with patch("tools.metrics.prometheus_client.query", return_value={
            "status": "success", "data": {"result": [{"value": [0, "0.27"]}]}
        }):
            self.poll(raw_alert())
        data = self.store.load()
        incident_id = data["active_by_fingerprint"][self.fp]
        row = data["incidents"][incident_id]
        self.assertEqual(len(row["evidence"]), 1)
        self.assertEqual(row["evidence"][0]["source"], "query_metrics")
        self.assertEqual(json.loads(row["evidence"][0]["detail"])["value"], 0.27)
        self.assertEqual(len(row["investigations"]), 1)
        self.assertIsNotNone(row["last_investigated_at"])

    def test_duplicate_investigating_alert_reuses_incident_id(self):
        incident = self.seed(last_at=None)
        with patch("monitor.incident_monitor.create_incident_from_alert") as factory:
            self.poll(raw_alert(), raw_alert())
        factory.assert_not_called()
        self.assertEqual(self.store.get_active_incident(self.fp).incident_id, incident.incident_id)
        self.assertEqual(len(self.store.load()["incidents"]), 1)
        self.assertEqual(self.sdk.responses.create.call_count, 1)

    def test_stable_alert_before_cooldown_skips_investigation(self):
        incident = self.seed(last_at=NOW - timedelta(minutes=1))
        self.poll(raw_alert(value=0.30))
        self.sdk.responses.create.assert_not_called()
        self.assertEqual(self.store.get_active_incident(self.fp).incident_id, incident.incident_id)

    def test_cooldown_boundary_reinvestigates_same_incident(self):
        incident = self.seed(last_at=NOW - timedelta(minutes=REINVESTIGATE_COOLDOWN_MINUTES))
        self.poll(raw_alert())
        self.assertEqual(self.sdk.responses.create.call_count, 1)
        reloaded = self.store.get_active_incident(self.fp)
        self.assertEqual(reloaded.incident_id, incident.incident_id)
        self.assertEqual(len(reloaded.alerts), 2)
        self.assertEqual(len(reloaded.investigations), 1)

    def test_significant_http_5xx_worsening_reinvestigates_early(self):
        incident = self.seed(value=0.20, last_at=NOW - timedelta(seconds=1))
        self.poll(raw_alert(value=0.50))
        self.assertEqual(self.sdk.responses.create.call_count, 1)
        reloaded = self.store.get_active_incident(self.fp)
        self.assertEqual(reloaded.incident_id, incident.incident_id)
        self.assertEqual(reloaded.last_investigated_value, 0.50)

    def test_exact_decimal_worsening_threshold_reinvestigates(self):
        self.assertEqual(HTTP_5XX_RATE_INCREASE_THRESHOLD, 0.2)
        incident = self.seed(value=0.50, last_at=NOW - timedelta(seconds=1))
        self.poll(raw_alert(value=0.70))
        self.assertEqual(self.sdk.responses.create.call_count, 1,
                         "0.50 -> 0.70 reaches the configured 0.20 threshold")
        self.assertEqual(self.store.get_active_incident(self.fp).incident_id, incident.incident_id)

    def test_other_metrics_use_cooldown_instead_of_http_5xx_threshold(self):
        incident = self.seed(value=0.27, last_at=NOW)
        alert = Alert(service="order-service", metric="http_request_rate", value=50, threshold=10, severity="critical")
        self.assertFalse(reinvestigation.should_reinvestigate(incident, alert))

    def test_missing_firing_alert_resolves_incident(self):
        incident = self.seed()
        self.poll()
        data = self.store.load()
        row = data["incidents"][incident.incident_id]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["incident_id"], incident.incident_id)
        self.assertNotIn(self.fp, data["active_by_fingerprint"])
        self.assertIsNone(self.store.get_active_incident(self.fp))

    def test_pending_alert_keeps_previously_firing_incident_active(self):
        incident = self.seed()
        self.poll(raw_alert(state="pending"))
        row = self.store.load()["incidents"][incident.incident_id]
        self.assertEqual(row["status"], "investigating")
        self.assertEqual(row["alert_state"], "pending")
        self.sdk.responses.create.assert_not_called()

    def test_refiring_after_resolution_preserves_old_incident_history(self):
        self.poll(raw_alert())
        first_incident = self.store.get_active_incident(self.fp)
        self.assertIsNotNone(first_incident)
        first_id = first_incident.incident_id
        self.assertEqual(first_incident.status, "investigating")
        data = self.store.load()
        self.assertEqual(set(data["incidents"]), {first_id})
        self.assertEqual(data["active_by_fingerprint"], {self.fp: first_id})

        self.poll()
        data = self.store.load()
        self.assertEqual(data["incidents"][first_id]["status"], "resolved")
        self.assertNotIn(self.fp, data["active_by_fingerprint"])
        self.assertIsNone(self.store.get_active_incident(self.fp))
        resolved_row = data["incidents"][first_id]

        self.poll(raw_alert(value=0.40))
        refired_incident = self.store.get_active_incident(self.fp)
        self.assertIsNotNone(refired_incident)
        second_id = refired_incident.incident_id
        self.assertNotEqual(second_id, first_id)
        data = self.store.load()
        self.assertEqual(set(data["incidents"]), {first_id, second_id})
        self.assertEqual(data["incidents"][first_id]["status"], "resolved")
        self.assertEqual(data["incidents"][second_id]["status"], "investigating")
        self.assertEqual(data["incidents"][first_id], resolved_row)
        self.assertEqual(data["active_by_fingerprint"], {self.fp: second_id})
        self.assertEqual(len(refired_incident.investigations), 1)
        self.assertEqual(refired_incident.alerts[0].value, 0.40)
        self.assertEqual(self.sdk.responses.create.call_count, 2)

    def test_completed_result_is_successful(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        result = self.agent.investigate(incident)
        self.assertTrue(result.success)
        self.assertEqual(result.status, InvestigationStatus.COMPLETED)
        self.assertEqual(result.answer, "Test investigation completed.")

    def test_max_round_result_is_failure_and_is_saved(self):
        self.sdk.responses.create.return_value = model_response(answer=None, calls=[tool_call()])
        with patch("tools.metrics.prometheus_client.query", return_value={
            "status": "success", "data": {"result": [{"value": [0, "0.27"]}]}
        }) as query:
            self.poll(raw_alert())
        self.assertEqual(self.sdk.responses.create.call_count, MAX_AGENT_LOOPS)
        self.assertEqual(query.call_count, 1)
        incident = self.store.get_active_incident(self.fp)
        self.assertEqual(incident.investigations[0].status, InvestigationStatus.MAX_ROUND_REACHED)
        self.assertIsNone(incident.last_investigated_at)
        self.assertIsNone(incident.last_investigated_value)
        self.assertEqual(len(incident.evidence), 1)

    def test_failed_reinvestigation_preserves_previous_success_timestamp_and_value(self):
        previous = NOW - timedelta(minutes=REINVESTIGATE_COOLDOWN_MINUTES)
        incident = self.seed(last_at=previous)
        self.sdk.responses.create.return_value = model_response(answer=None, calls=[tool_call(name="missing")])
        self.poll(raw_alert(value=0.30))
        row = self.store.get_active_incident(self.fp)
        self.assertEqual(row.incident_id, incident.incident_id)
        self.assertEqual(row.last_investigated_at, previous)
        self.assertEqual(row.last_investigated_value, 0.27)
        self.assertEqual(row.investigations[0].status, InvestigationStatus.MAX_ROUND_REACHED)

    def test_successful_tool_output_becomes_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        result, query = self.investigate_with_metrics(incident)
        self.assertTrue(result.success)
        query.assert_called_once()
        self.assertEqual(len(incident.evidence), 1)

    def test_no_data_does_not_become_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        self.investigate_with_metrics(incident, empty=True)
        self.assertEqual(incident.evidence, [])
        outputs = [i for i in self.agent.history if isinstance(i, dict) and i.get("type") == "function_call_output"]
        self.assertEqual(json.loads(outputs[0]["output"])["status"], "no_data")

    def test_undefined_metric_does_not_become_valid_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        self.investigate_with_metrics(incident, value="NaN")
        self.assertEqual(incident.evidence, [], "An undefined 0/0 rate must not be stored as successful metric evidence")

    def test_identical_evidence_across_investigations_is_not_duplicated(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        self.investigate_with_metrics(incident)
        self.investigate_with_metrics(incident)
        self.assertEqual(len(incident.evidence), 1)

    def test_changed_evidence_across_investigations_is_added(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        self.investigate_with_metrics(incident, value=0.27)
        self.investigate_with_metrics(incident, value=0.40)
        self.assertEqual(len(incident.evidence), 2)
        self.assertEqual([json.loads(e.detail)["value"] for e in incident.evidence], [0.27, 0.40])

    def test_evidence_key_order_does_not_defeat_deduplication(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        item = SimpleNamespace(name="query_metrics")
        self.agent.add_evidence(incident, item, {"status": "ok", "value": 0.27}, True, False)
        self.agent.add_evidence(incident, item, {"value": 0.27, "status": "ok"}, True, False)
        self.assertEqual(len(incident.evidence), 1)

    def test_failed_tools_do_not_become_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()), self.fp)
        self.sdk.responses.create.side_effect = [model_response(answer=None, calls=[tool_call()]), model_response()]
        with patch("tools.metrics.prometheus_client.query", side_effect=RuntimeError("test tool unavailable")):
            self.agent.investigate(incident)
        self.assertEqual(incident.evidence, [])

    def test_prometheus_request_failure_is_logged_and_polling_recovers(self):
        incident = self.seed()
        active_response = {"status": "success", "data": {"alerts": [raw_alert()]}}
        observed = []

        def next_response():
            if not observed:
                observed.append("failure")
                raise ConnectionError("test Prometheus unavailable")
            self.assertEqual(self.store.get_active_incident(self.fp).status, "investigating")
            observed.append("recovered")
            return active_response

        self.prometheus.get_alerts.side_effect = next_response
        with self.assertLogs("agent", level="ERROR") as captured, patch(
            "monitor.incident_monitor.time.sleep", side_effect=[None, KeyboardInterrupt]
        ):
            self.monitor.run()
        self.assertEqual(observed, ["failure", "recovered"])
        self.assertIn("test Prometheus unavailable", "\n".join(captured.output))
        self.assertEqual(self.store.get_active_incident(self.fp).incident_id, incident.incident_id)

    def test_agent_exception_is_logged_and_next_poll_obeys_retry_delay(self):
        self.prometheus.get_alerts.return_value = {"status": "success", "data": {"alerts": [raw_alert()]}}
        self.sdk.responses.create.side_effect = [RuntimeError("test investigation failed"), model_response()]
        with self.assertLogs("agent", level="ERROR") as captured, patch(
            "monitor.incident_monitor.time.sleep", side_effect=[None, KeyboardInterrupt]
        ):
            self.monitor.run()
        self.assertEqual(self.prometheus.get_alerts.call_count, 2)
        self.assertEqual(self.sdk.responses.create.call_count, 1)
        self.assertEqual(len(self.store.load()["incidents"]), 1)
        self.assertEqual(len(self.store.get_active_incident(self.fp).investigations), 0)
        self.assertIn("test investigation failed", "\n".join(captured.output))

    def test_one_agent_exception_does_not_block_other_alerts(self):
        self.sdk.responses.create.side_effect = [RuntimeError("test first investigation failed"), model_response()]
        with self.assertLogs("agent", level="ERROR"):
            self.poll(raw_alert(), raw_alert(service="inventory-service"))
        rows = list(self.store.load()["incidents"].values())
        self.assertEqual(len(rows), 2)
        self.assertEqual([len(row["investigations"]) for row in rows], [0, 1])

    def test_agent_exception_preserves_previous_success_timestamp(self):
        previous = NOW - timedelta(minutes=REINVESTIGATE_COOLDOWN_MINUTES)
        self.seed(last_at=previous)
        self.sdk.responses.create.side_effect = RuntimeError("test investigation failed")
        with self.assertLogs("agent", level="ERROR"):
            self.poll(raw_alert())
        self.assertEqual(self.store.get_active_incident(self.fp).last_investigated_at, previous)

    def test_successful_evidence_survives_later_model_exception(self):
        self.sdk.responses.create.side_effect = [
            model_response(answer=None, calls=[tool_call()]), RuntimeError("test model failed after evidence")
        ]
        with patch("tools.metrics.prometheus_client.query", return_value={
            "status": "success", "data": {"result": [{"value": [0, "0.27"]}]}
        }), self.assertLogs("agent", level="ERROR"):
            self.poll(raw_alert())
        incident = self.store.get_active_incident(self.fp)
        self.assertIsNone(incident.last_investigated_at)
        self.assertEqual(len(incident.evidence), 1, "Successful evidence must survive a later model exception")

    def test_incomplete_model_response_does_not_mark_investigation_successful(self):
        self.sdk.responses.create.return_value = model_response(answer=None, status="incomplete")
        self.poll(raw_alert())
        incident = self.store.get_active_incident(self.fp)
        self.assertIsNone(incident.last_investigated_at, "An incomplete model response must not update success timestamp")
        self.assertNotEqual(incident.investigations[0].status, InvestigationStatus.COMPLETED)

    def test_invalid_alert_snapshot_does_not_falsely_resolve_active_incident(self):
        self.seed()
        self.prometheus.get_alerts.return_value = {"status": "success", "data": {}}
        try:
            self.monitor.process_alerts_once()
        except (ValueError, KeyError):
            pass
        self.assertEqual(self.store.get_active_incident(self.fp).status, "investigating",
                         "A response without data.alerts is not a valid empty alert snapshot")

    def test_store_roundtrip_retains_evidence_and_investigations(self):
        self.poll(raw_alert())
        reloaded = IncidentStore(self.store.file_path).get_active_incident(self.fp)
        self.assertIsInstance(reloaded, Incident)
        self.assertEqual(reloaded.investigations[0].status, InvestigationStatus.COMPLETED)
        self.assertEqual(reloaded.last_investigated_at, NOW)

    def test_store_load_accepts_existing_null_evidence_and_investigations(self):
        incident = self.seed()
        data = self.store.load()
        data["incidents"][incident.incident_id]["evidence"] = None
        data["incidents"][incident.incident_id]["investigations"] = None
        self.store.save(data)
        reloaded = self.store.get_active_incident(self.fp)
        self.assertEqual(reloaded.evidence, [])
        self.assertEqual(reloaded.investigations, [])


class MetricsRegressionTests(unittest.TestCase):
    def setUp(self):
        from services.configuration import SERVICES
        from services.registry import ServiceRegistry
        from copy import deepcopy

        self.definitions = deepcopy(SERVICES)
        self.definitions.append(
            {
                "name": "http-target",
                "type": "http_service",
                "metrics": {
                    "provider": "prometheus",
                    "profile": "http_service",
                },
            }
        )
        self.registry_patch = self.enterContext(
            patch("tools.metrics.registry", ServiceRegistry(self.definitions))
        )
        self.query = self.enterContext(
            patch(
                "tools.metrics.prometheus_client.query",
                return_value={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"value": [0, "1"]}],
                    },
                },
            )
        )

    def assert_query(self, metric, promql, service="order-db"):
        result = query_metrics(service, metric)
        self.query.assert_called_once_with(promql)
        self.assertEqual(result, {
            "status": "ok", "service": service, "metric": metric, "value": 1.0, "promql": promql,
        })
        return result

    def test_http_metrics_keep_original_promql(self):
        expected = {
            "http_requests_total": 'sum(demo_http_requests_total{service="order-service"})',
            "http_request_rate": 'sum(rate(demo_http_requests_total{service="order-service"}[1m]))',
            "http_5xx_rate": (
                '((sum(rate(demo_http_requests_total{service="order-service",status=~"5[0-9][0-9]"}[1m])) '
                'or (0 * sum(rate(demo_http_requests_total{service="order-service"}[1m])))) / '
                'sum(rate(demo_http_requests_total{service="order-service"}[1m]))) '
                'and (sum(increase(demo_http_requests_total{service="order-service"}[1m])) >= 20)'
                ' and (min(up{service="order-service"}) == 1)'
            ),
        }
        for metric, promql in expected.items():
            with self.subTest(metric=metric):
                self.query.reset_mock()
                self.assert_query(metric, promql, service="order-service")

    def test_postgres_up_uses_configured_job(self):
        self.assert_query("postgres_up", 'pg_up{job="postgres"} and on (job, instance) (up{job="postgres"} == 1)')

    def test_postgres_connections_use_configured_database(self):
        self.assert_query(
            "postgres_connections", 'sum(pg_stat_database_numbackends{job="postgres",datname="orders"})'
        )

    def test_postgres_max_connections_are_instance_level(self):
        self.assert_query("postgres_max_connections", 'pg_settings_max_connections{job="postgres"}')

    def test_postgres_connection_usage_counts_all_databases_per_instance(self):
        self.assert_query("postgres_connection_usage", (
            'sum by (job, instance) (pg_stat_database_numbackends{job="postgres",datname!=""})'
            ' / on (job, instance) pg_settings_max_connections{job="postgres"}'
        ))

    def test_postgres_connection_usage_preserves_fraction(self):
        self.query.return_value["data"]["result"] = [{"value": [0, "0.12"]}]
        result = query_metrics("order-db", "postgres_connection_usage")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], 0.12)
        self.assertIsInstance(result["value"], float)

    def test_postgres_active_sessions(self):
        self.assert_query("postgres_active_sessions", (
            'sum(pg_stat_activity_count{job="postgres",datname="orders",state="active"})'
        ))

    def test_postgres_idle_sessions(self):
        self.assert_query("postgres_idle_sessions", (
            'sum(pg_stat_activity_count{job="postgres",datname="orders",state="idle"})'
        ))

    def test_postgres_idle_in_transaction(self):
        self.assert_query("postgres_idle_in_transaction", (
            'sum(pg_stat_activity_count{job="postgres",datname="orders",state="idle in transaction"})'
        ))

    def test_postgres_lock_waits_keep_activity_fallback_and_database_gate(self):
        self.assert_query("postgres_lock_waits", (
            '(sum by (job, instance) ('
            'pg_stat_activity_count{job="postgres",datname="orders",wait_event_type="Lock"}'
            ') or on (job, instance) (0 * sum by (job, instance) ('
            'pg_stat_activity_count{job="postgres",datname="orders"}'
            '))) and on (job, instance) (pg_up{job="postgres"} == 1)'
        ))

    def test_postgres_lock_waits_zero_is_valid_data(self):
        self.query.return_value["data"]["result"] = [{"value": [0, "0"]}]
        result = query_metrics("order-db", "postgres_lock_waits")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], 0.0)

    def test_postgres_lock_waits_empty_result_is_no_data(self):
        self.query.return_value["data"]["result"] = []
        result = query_metrics("order-db", "postgres_lock_waits")
        self.assertEqual(result["status"], "no_data")
        self.assertNotIn("value", result)

    def test_postgres_targets_are_not_hardcoded(self):
        from services.registry import ServiceRegistry

        self.definitions.append(
            {
                "name": "inventory-db",
                "type": "postgresql",
                "metrics": {
                    "provider": "prometheus",
                    "profile": "postgresql",
                    "job": "inventory",
                    "database": "stock",
                },
            }
        )
        import tools.metrics

        tools.metrics.registry = ServiceRegistry(self.definitions)
        self.assert_query(
            "postgres_connections",
            ('sum(pg_stat_database_numbackends{job="inventory",datname="stock"})'),
            service="inventory-db",
        )

    def test_postgres_label_values_are_escaped(self):
        from services.registry import ServiceRegistry

        self.definitions[1]["metrics"]["job"] = 'post"gres\\east'
        self.definitions[1]["metrics"]["database"] = 'order"books'
        import tools.metrics

        tools.metrics.registry = ServiceRegistry(self.definitions)
        self.assert_query(
            "postgres_connections",
            (
                r'sum(pg_stat_database_numbackends{job="post\"gres\\east",datname="order\"books"})'
            ),
        )

    def test_missing_postgres_target_returns_no_data_without_query(self):
        result = query_metrics("missing-db", "postgres_up")
        self.assertEqual(result["status"], "no_data")
        self.assertIn("没有找到", result["message"])
        self.query.assert_not_called()

    def test_non_postgres_target_returns_no_data_without_query(self):
        result = query_metrics("http-target", "postgres_connections")
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["reason"], "metric_not_supported_by_profile")
        self.query.assert_not_called()

    def test_incomplete_postgres_target_returns_clear_configuration_error(self):
        from services.registry import ServiceRegistry
        from pydantic import ValidationError
        from copy import deepcopy

        for field in ("job", "database"):
            for value in (None, "", " ", 10):
                with self.subTest(field=field, value=value):
                    definitions = deepcopy(self.definitions)
                    definitions[1]["metrics"][field] = value
                    with self.assertRaises(ValidationError) as caught:
                        ServiceRegistry(definitions)
                    self.assertIn(field, str(caught.exception))
                    self.query.assert_not_called()

    def test_unsupported_metrics_return_no_data_without_query(self):
        for metric in ("unsupported", "postgres_unsupported", "pg_stat_statements"):
            with self.subTest(metric=metric):
                result = query_metrics("order-db", metric)
                self.assertEqual(result["status"], "no_data")
                self.assertIn("不支持", result["message"])
                self.query.assert_not_called()

    def test_empty_prometheus_result_returns_no_data(self):
        self.query.return_value["data"]["result"] = []
        result = query_metrics("order-db", "postgres_connections")
        self.assertEqual(result["status"], "no_data")
        self.assertNotIn("value", result)

    def test_non_finite_prometheus_values_return_no_data(self):
        for value in ("NaN", "Infinity", "+Infinity", "-Infinity", "+Inf", "-Inf"):
            with self.subTest(value=value):
                self.query.return_value["data"]["result"] = [{"value": [0, value]}]
                result = query_metrics("order-db", "postgres_connection_usage")
                self.assertEqual(result["status"], "no_data")
                self.assertNotIn("value", result)
                self.assertIn("有限数值", result["message"])

    def test_finite_values_including_postgres_up_zero_remain_valid(self):
        for metric, value in (
            ("postgres_up", "0"), ("postgres_up", "0.0"), ("postgres_up", "1"),
            ("postgres_max_connections", "100"), ("postgres_connection_usage", "0.12"),
        ):
            with self.subTest(metric=metric, value=value):
                self.query.return_value["data"]["result"] = [{"value": [0, value]}]
                result = query_metrics("order-db", metric)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["value"], float(value))
                self.assertIsInstance(result["value"], float)

    def test_multiple_results_are_rejected_instead_of_taking_first(self):
        self.query.return_value["data"]["result"] = [
            {"metric": {"job": "postgres", "instance": "first"}, "value": [0, "1"]},
            {"metric": {"job": "postgres", "instance": "second"}, "value": [0, "0"]},
        ]
        for metric in (
            "postgres_up", "postgres_max_connections", "postgres_connection_usage", "postgres_lock_waits",
        ):
            with self.subTest(metric=metric):
                result = query_metrics("order-db", metric)
                self.assertEqual(result["status"], "no_data")
                self.assertNotIn("value", result)
                self.assertIn("多个结果", result["message"])
                self.assertIn("promql", result)

    def test_metrics_tool_schema_keeps_interface_and_complete_supported_list(self):
        schema = tool_registry["query_metrics"]["schema"]
        self.assertEqual(schema["parameters"]["required"], ["service", "metric"])
        properties = schema["parameters"]["properties"]
        self.assertEqual(set(properties), {"service", "metric"})
        self.assertEqual(properties["service"]["type"], "string")
        self.assertEqual(properties["metric"]["type"], "string")
        for metric in (
            "http_requests_total", "http_request_rate", "http_5xx_rate", "postgres_up",
            "postgres_connections", "postgres_max_connections", "postgres_connection_usage",
            "postgres_active_sessions", "postgres_idle_sessions", "postgres_idle_in_transaction",
            "postgres_lock_waits",
        ):
            self.assertIn(metric, properties["metric"]["description"])
        self.assertNotIn("build_postgres_promql", tool_registry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
