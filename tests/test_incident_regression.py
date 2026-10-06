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
from datetime import datetime, timedelta
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
    import reinvestigation


NOW = datetime(2026, 10, 6, 14, 30, 0)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


def raw_alert(value=0.27, service="order-service", metric="http_5xx_rate", state="firing"):
    return {
        "state": state,
        "labels": {"service": service, "metric": metric, "severity": "critical"},
        "annotations": {"threshold": "0.20"},
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
        self.enterContext(patch("monitor.incident_monitor.datetime", FixedDatetime))
        self.enterContext(patch("reinvestigation.datetime", FixedDatetime))

    def poll(self, *alerts):
        self.prometheus.get_alerts.return_value = {"status": "success", "data": {"alerts": list(alerts)}}
        self.monitor.process_alerts_once()

    def seed(self, value=0.27, last_at=NOW, last_value=None):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert(value)))
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
            }) as get_alerts, patch("monitor.incident_monitor.time.sleep", side_effect=[None, KeyboardInterrupt]):
                namespace = runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
            self.assertIsInstance(namespace["agent"], Agent)
            self.assertIsInstance(namespace["prometheus_client"], PrometheusClient)
            self.assertIsInstance(namespace["incident_store"], IncidentStore)
            self.assertIsInstance(namespace["monitor"], IncidentMonitor)
            self.assertEqual(get_alerts.call_count, 2)
            self.assertEqual(json.loads((self.work / "data" / "incidents.json").read_text()), {})
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
        self.assertEqual(self.store.load(), {})
        self.sdk.responses.create.assert_not_called()

    def test_fingerprint_is_stable_across_value_and_severity_changes(self):
        changed = raw_alert(value=0.8)
        changed["labels"]["severity"] = "warning"
        changed["annotations"]["threshold"] = "0.10"
        self.assertEqual(generate_alert_fingerprint(parse_prometheus_alert(changed)), self.fp)
        self.assertEqual(self.fp, hashlib.sha256(b"order-service|http_5xx_rate").hexdigest())

    def test_fingerprint_separates_different_services_and_metrics(self):
        self.assertNotEqual(generate_alert_fingerprint(parse_prometheus_alert(raw_alert(service="other"))), self.fp)
        self.assertNotEqual(generate_alert_fingerprint(parse_prometheus_alert(raw_alert(metric="http_request_rate"))), self.fp)

    def test_new_incident_is_saved_before_real_agent_starts(self):
        seen = []

        def model_call(**kwargs):
            row = self.store.load()[self.fp]
            seen.append(row["incident_id"])
            self.assertEqual(row["status"], "investigating")
            self.assertEqual(row["alerts"][0]["value"], 0.27)
            self.assertEqual({t["name"] for t in kwargs["tools"]},
                             {"query_logs", "query_metrics", "check_service_health"})
            return model_response()

        self.sdk.responses.create.side_effect = model_call
        self.poll(raw_alert())
        row = self.store.load()[self.fp]
        self.assertEqual(seen, [row["incident_id"]])
        self.assertRegex(row["incident_id"], r"^inc-[0-9a-f]{8}$")
        self.assertEqual(row["investigations"][0]["status"], "completed")

    def test_new_incident_tool_results_and_investigation_persist(self):
        self.sdk.responses.create.side_effect = [
            model_response(answer=None, calls=[tool_call()], reasoning=True), model_response()
        ]
        with patch("tools.metrics.prometheus_client.query", return_value={
            "status": "success", "data": {"result": [{"value": [0, "0.27"]}]}
        }):
            self.poll(raw_alert())
        row = self.store.load()[self.fp]
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
        self.assertEqual(self.store.load()[self.fp]["incident_id"], incident.incident_id)
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.sdk.responses.create.call_count, 1)

    def test_stable_alert_before_cooldown_skips_investigation(self):
        incident = self.seed(last_at=NOW - timedelta(minutes=1))
        self.poll(raw_alert(value=0.30))
        self.sdk.responses.create.assert_not_called()
        self.assertEqual(self.store.load()[self.fp]["incident_id"], incident.incident_id)

    def test_cooldown_boundary_reinvestigates_same_incident(self):
        incident = self.seed(last_at=NOW - timedelta(minutes=REINVESTIGATE_COOLDOWN_MINUTES))
        self.poll(raw_alert())
        self.assertEqual(self.sdk.responses.create.call_count, 1)
        row = self.store.load()[self.fp]
        self.assertEqual(row["incident_id"], incident.incident_id)
        self.assertEqual(len(row["alerts"]), 2)
        self.assertEqual(len(row["investigations"]), 1)

    def test_significant_http_5xx_worsening_reinvestigates_early(self):
        incident = self.seed(value=0.20, last_at=NOW - timedelta(seconds=1))
        self.poll(raw_alert(value=0.50))
        self.assertEqual(self.sdk.responses.create.call_count, 1)
        self.assertEqual(self.store.load()[self.fp]["incident_id"], incident.incident_id)
        self.assertEqual(self.store.load()[self.fp]["last_investigated_value"], 0.50)

    def test_exact_decimal_worsening_threshold_reinvestigates(self):
        self.assertEqual(HTTP_5XX_RATE_INCREASE_THRESHOLD, 0.2)
        incident = self.seed(value=0.50, last_at=NOW - timedelta(seconds=1))
        self.poll(raw_alert(value=0.70))
        self.assertEqual(self.sdk.responses.create.call_count, 1,
                         "0.50 -> 0.70 reaches the configured 0.20 threshold")
        self.assertEqual(self.store.load()[self.fp]["incident_id"], incident.incident_id)

    def test_other_metrics_use_cooldown_instead_of_http_5xx_threshold(self):
        incident = self.seed(value=0.27, last_at=NOW)
        alert = parse_prometheus_alert(raw_alert(value=50, metric="http_request_rate"))
        self.assertFalse(reinvestigation.should_reinvestigate(incident, alert))

    def test_missing_firing_alert_resolves_incident(self):
        incident = self.seed()
        self.poll()
        row = self.store.load()[self.fp]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["incident_id"], incident.incident_id)
        self.assertIsNone(self.store.get_active_incident(self.fp))

    def test_pending_alert_resolves_previously_firing_incident(self):
        self.seed()
        self.poll(raw_alert(state="pending"))
        self.assertEqual(self.store.load()[self.fp]["status"], "resolved")

    def test_refiring_after_resolution_creates_new_incident_id(self):
        incident = self.seed()
        self.poll()
        self.poll(raw_alert())
        row = self.store.load()[self.fp]
        self.assertNotEqual(row["incident_id"], incident.incident_id)
        self.assertEqual(row["status"], "investigating")
        self.assertEqual(len(row["investigations"]), 1)

    def test_completed_result_is_successful(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
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
        row = self.store.load()[self.fp]
        self.assertEqual(row["investigations"][0]["status"], "max_round_reached")
        self.assertIsNone(row["last_investigated_at"])
        self.assertIsNone(row["last_investigated_value"])
        self.assertEqual(len(row["evidence"]), 1)

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
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
        result, query = self.investigate_with_metrics(incident)
        self.assertTrue(result.success)
        query.assert_called_once()
        self.assertEqual(len(incident.evidence), 1)

    def test_no_data_does_not_become_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
        self.investigate_with_metrics(incident, empty=True)
        self.assertEqual(incident.evidence, [])
        outputs = [i for i in self.agent.history if isinstance(i, dict) and i.get("type") == "function_call_output"]
        self.assertEqual(json.loads(outputs[0]["output"])["status"], "no_data")

    def test_undefined_metric_does_not_become_valid_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
        self.investigate_with_metrics(incident, value="NaN")
        self.assertEqual(incident.evidence, [], "An undefined 0/0 rate must not be stored as successful metric evidence")

    def test_identical_evidence_across_investigations_is_not_duplicated(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
        self.investigate_with_metrics(incident)
        self.investigate_with_metrics(incident)
        self.assertEqual(len(incident.evidence), 1)

    def test_changed_evidence_across_investigations_is_added(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
        self.investigate_with_metrics(incident, value=0.27)
        self.investigate_with_metrics(incident, value=0.40)
        self.assertEqual(len(incident.evidence), 2)
        self.assertEqual([json.loads(e.detail)["value"] for e in incident.evidence], [0.27, 0.40])

    def test_evidence_key_order_does_not_defeat_deduplication(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
        item = SimpleNamespace(name="query_metrics")
        self.agent.add_evidence(incident, item, {"status": "ok", "value": 0.27}, True, False)
        self.agent.add_evidence(incident, item, {"value": 0.27, "status": "ok"}, True, False)
        self.assertEqual(len(incident.evidence), 1)

    def test_failed_tools_do_not_become_evidence(self):
        incident = create_incident_from_alert(parse_prometheus_alert(raw_alert()))
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
            self.assertEqual(self.store.load()[self.fp]["status"], "investigating")
            observed.append("recovered")
            return active_response

        self.prometheus.get_alerts.side_effect = next_response
        with self.assertLogs("agent", level="ERROR") as captured, patch(
            "monitor.incident_monitor.time.sleep", side_effect=[None, KeyboardInterrupt]
        ):
            self.monitor.run()
        self.assertEqual(observed, ["failure", "recovered"])
        self.assertIn("test Prometheus unavailable", "\n".join(captured.output))
        self.assertEqual(self.store.load()[self.fp]["incident_id"], incident.incident_id)

    def test_agent_exception_is_logged_and_next_poll_retries_same_incident(self):
        self.prometheus.get_alerts.return_value = {"status": "success", "data": {"alerts": [raw_alert()]}}
        self.sdk.responses.create.side_effect = [RuntimeError("test investigation failed"), model_response()]
        with self.assertLogs("agent", level="ERROR") as captured, patch(
            "monitor.incident_monitor.time.sleep", side_effect=[None, KeyboardInterrupt]
        ):
            self.monitor.run()
        self.assertEqual(self.prometheus.get_alerts.call_count, 2)
        self.assertEqual(self.sdk.responses.create.call_count, 2)
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(len(self.store.load()[self.fp]["investigations"]), 1)
        self.assertIn("test investigation failed", "\n".join(captured.output))

    def test_one_agent_exception_does_not_block_other_alerts(self):
        self.sdk.responses.create.side_effect = [RuntimeError("test first investigation failed"), model_response()]
        with self.assertLogs("agent", level="ERROR"):
            self.poll(raw_alert(), raw_alert(service="second-service"))
        rows = list(self.store.load().values())
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
        row = self.store.load()[self.fp]
        self.assertIsNone(row["last_investigated_at"])
        self.assertEqual(len(row["evidence"]), 1, "Successful evidence must survive a later model exception")

    def test_incomplete_model_response_does_not_mark_investigation_successful(self):
        self.sdk.responses.create.return_value = model_response(answer=None, status="incomplete")
        self.poll(raw_alert())
        row = self.store.load()[self.fp]
        self.assertIsNone(row["last_investigated_at"], "An incomplete model response must not update success timestamp")
        self.assertNotEqual(row["investigations"][0]["status"], "completed")

    def test_invalid_alert_snapshot_does_not_falsely_resolve_active_incident(self):
        self.seed()
        self.prometheus.get_alerts.return_value = {"status": "success", "data": {}}
        try:
            self.monitor.process_alerts_once()
        except (ValueError, KeyError):
            pass
        self.assertEqual(self.store.load()[self.fp]["status"], "investigating",
                         "A response without data.alerts is not a valid empty alert snapshot")

    def test_store_roundtrip_retains_evidence_and_investigations(self):
        self.poll(raw_alert())
        reloaded = IncidentStore(self.store.file_path).get_active_incident(self.fp)
        self.assertIsInstance(reloaded, Incident)
        self.assertEqual(reloaded.investigations[0].status, InvestigationStatus.COMPLETED)
        self.assertEqual(reloaded.last_investigated_at, NOW)

    def test_store_load_accepts_existing_null_evidence_and_investigations(self):
        self.seed()
        rows = self.store.load()
        rows[self.fp]["evidence"] = None
        rows[self.fp]["investigations"] = None
        self.store.save(rows)
        reloaded = self.store.get_active_incident(self.fp)
        self.assertEqual(reloaded.evidence, [])
        self.assertEqual(reloaded.investigations, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
