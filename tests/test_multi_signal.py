"""Unit/Mock coverage; live integrations are explicitly separate."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from alert_parser import normalize_prometheus_alert, normalize_snapshot
from alert_policy import ALERT_POLICIES
from alert_correlation import correlate_alerts
from alert_recovery import can_clear, present_value, rules_are_current
from fingerprint import generate_alert_fingerprint, legacy_fingerprint
from incident_factory import create_incident_from_alert
from incident_store import IncidentStore
from models.incident import AlertEvent, Evidence, Incident
from models.investigation import InvestigationResult, InvestigationStatus
from monitor.incident_monitor import IncidentMonitor
from reinvestigation import has_alert_worsened, should_reinvestigate
from services.metric_profiles import build_promql
from services.registry import registry
from remediation.planner import RemediationPlanner
from remediation.store import RemediationStore


NOW = datetime.now(timezone.utc)


def raw(name="HighHttp5xxRate", service=None, value=None, state="firing"):
    policy = ALERT_POLICIES[name]
    service = service or (
        "order-db" if policy.metric_profile == "postgresql" else "order-service"
    )
    labels = {
        "alertname": name,
        "service": service,
        "metric": policy.metric,
        "severity": policy.severity,
        "signal_type": policy.signal_type,
    }
    if policy.identity_labels:
        labels.update(
            {
                "job": "postgres" if service == "order-db" else "demo-python",
                "instance": "host.docker.internal:9187"
                if service == "order-db"
                else "host.docker.internal:8001",
            }
        )
    annotations = {
        "threshold": "1"
        if policy.signal_type == "state"
        else "0.2"
        if name == "HighHttp5xxRate"
        else "0.5",
        "direction": policy.direction,
        "unit": policy.unit,
    }
    if policy.window:
        annotations.update(
            {
                "window": policy.window,
                "minimum_requests": str(policy.minimum_requests),
            }
        )
    return {
        "labels": labels,
        "annotations": annotations,
        "state": state,
        "value": str(
            value if value is not None else 0 if policy.signal_type == "state" else 0.8
        ),
        "activeAt": NOW.isoformat(),
    }


def event(*args, **kwargs):
    return normalize_prometheus_alert(raw(*args, **kwargs)).alert


def envelope(*entries, **extra):
    return {
        "status": "success",
        "data": {"alerts": list(entries)},
        **extra,
    }


def completed():
    return InvestigationResult(
        success=True, status=InvestigationStatus.COMPLETED, answer="mock"
    )


class PolicyAndEventTests(unittest.TestCase):
    def test_five_policies_have_consistent_metric_capabilities(self):
        self.assertEqual(len(ALERT_POLICIES), 5)
        for name, policy in ALERT_POLICIES.items():
            normalized = normalize_prometheus_alert(raw(name))
            self.assertEqual(normalized.outcome, "valid", name)
            self.assertEqual(normalized.alert.alertname, name)
            self.assertEqual(normalized.alert.unit, policy.unit)

    def test_rules_contract(self):
        import os
        import yaml

        path = Path(os.getenv("ALERT_RULES_PATH", "D:/prometheus-demo/alerts.yml"))
        if not path.is_file():
            self.skipTest(
                "Set ALERT_RULES_PATH for cross-repository contract validation"
            )
        rules = yaml.safe_load(path.read_text(encoding="utf-8"))["groups"][0]["rules"]
        self.assertEqual({r["alert"] for r in rules}, set(ALERT_POLICIES))
        for rule in rules:
            policy = ALERT_POLICIES[rule["alert"]]
            self.assertEqual(rule["labels"]["metric"], policy.metric)
            self.assertEqual(rule["labels"]["severity"], policy.severity)
            self.assertEqual(rule["labels"]["signal_type"], policy.signal_type)
            annotations = rule["annotations"]
            self.assertEqual(annotations["unit"], policy.unit)
            self.assertEqual(annotations["direction"], policy.direction)
            self.assertEqual(annotations.get("window"), policy.window)
            if policy.minimum_requests:
                self.assertIn(f">= {policy.minimum_requests}", rule["expr"])
                self.assertEqual(
                    annotations["minimum_requests"], str(policy.minimum_requests)
                )
            comparison = ">" if policy.direction == "higher_is_worse" else "<"
            self.assertIn(f'{comparison} {annotations["threshold"]}', rule["expr"])
            self.assertEqual(rule["for"], "10s")

    def test_source_paths_come_from_registry_configuration(self):
        self.assertEqual(
            registry.describe("inventory-service")["source_paths"],
            ["inventory_app.py", "http_observability.py"],
        )
        self.assertEqual(
            registry.describe("order-service")["source_paths"],
            ["app.py", "http_observability.py"],
        )

    def test_http_profiles_share_policy_window_and_guard(self):
        for name in ["HighHttp5xxRate", "HighHttpLatency"]:
            policy = ALERT_POLICIES[name]
            query = build_promql(
                "inventory-service",
                registry.get("inventory-service").metrics,
                policy.metric,
            )
            self.assertIn(f"[{policy.window}]", query)
            self.assertIn(f">= {policy.minimum_requests}", query)

    def test_nan_and_infinity_cannot_be_events(self):
        for value in ["NaN", "Infinity", "-Inf", True]:
            with self.assertRaises((ValidationError, ValueError)):
                AlertEvent(service="s", severity="critical", value=value)

    def test_unknown_value_stays_none_for_nonnumeric_event(self):
        item = AlertEvent(
            service="s",
            severity="info",
            alertname="FutureEvent",
            source="future",
            signal_type="event",
            direction="event",
        )
        self.assertIsNone(item.value)
        self.assertIsNone(item.threshold)

    def test_incomplete_legacy_alert_is_not_fabricated(self):
        with self.assertRaises(ValidationError):
            AlertEvent(
                service="s", severity="critical", metric="http_5xx_rate", threshold=0.2
            )

    def test_modern_numeric_event_requires_all_semantics(self):
        with self.assertRaises(ValidationError):
            AlertEvent(
                service="s",
                severity="critical",
                source="prometheus",
                alertname="HighHttp5xxRate",
            )

    def test_timezone_normalizes_to_utc(self):
        entry = raw()
        entry["activeAt"] = "2026-10-08T12:00:00+08:00"
        parsed = normalize_prometheus_alert(entry).alert
        self.assertEqual(parsed.starts_at.isoformat(), "2026-10-08T04:00:00+00:00")

    def test_naive_or_invalid_timestamp_rejected(self):
        for stamp in ["yesterday", "2026-10-08T12:00:00", 42, ""]:
            entry = raw()
            entry["activeAt"] = stamp
            self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")


class ParserTests(unittest.TestCase):
    def test_multiple_real_api_entries(self):
        snapshot = normalize_snapshot(envelope(*(raw(name) for name in ALERT_POLICIES)))
        self.assertTrue(snapshot.complete)
        self.assertEqual(len(snapshot.alerts), 5)

    def test_pending_and_inactive_distinct_from_invalid(self):
        snapshot = normalize_snapshot(
            envelope(raw(state="pending"), raw(state="inactive"))
        )
        self.assertTrue(snapshot.complete)
        self.assertEqual(len(snapshot.ignored), 2)
        self.assertEqual(snapshot.invalid, [])

    def test_bad_single_entry_does_not_hide_valid_entries(self):
        snapshot = normalize_snapshot(
            envelope(raw(), {"state": "firing"}, raw("HighHttpLatency"))
        )
        self.assertEqual(len(snapshot.alerts), 2)
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(snapshot.invalid), 1)

    def test_missing_required_fields_rejected(self):
        for key in ["service", "alertname", "metric", "severity", "signal_type"]:
            entry = raw()
            entry["labels"].pop(key)
            self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")
        for key in ["threshold", "unit", "direction", "window", "minimum_requests"]:
            entry = raw()
            entry["annotations"].pop(key)
            self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")

    def test_invalid_numeric_inputs_rejected(self):
        for value in [None, "", "oops", "NaN", "Inf", [], {}, True]:
            entry = raw()
            entry["value"] = value
            self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")

    def test_unknown_rule_and_service_rejected(self):
        for key, value in [("alertname", "Unknown"), ("service", "Unknown")]:
            entry = raw()
            entry["labels"][key] = value
            self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")

    def test_wrong_profile_and_metric_rejected(self):
        self.assertEqual(
            normalize_prometheus_alert(raw(service="order-db")).outcome, "invalid"
        )
        entry = raw()
        entry["labels"]["metric"] = "postgres_up"
        self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")

    def test_state_requires_target_identity(self):
        for key in ["instance", "job"]:
            entry = raw("ServiceDown")
            entry["labels"].pop(key)
            self.assertEqual(normalize_prometheus_alert(entry).outcome, "invalid")

    def test_invalid_envelopes_never_complete(self):
        for response in [
            None,
            [],
            {},
            {"status": "error"},
            {
                "status": "success",
                "data": {},
            },
            {
                "status": "success",
                "data": {"alerts": None},
            },
        ]:
            self.assertFalse(normalize_snapshot(response).complete)

    def test_warning_snapshot_is_incomplete(self):
        snapshot = normalize_snapshot(envelope(raw(), warnings=["partial data"]))
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(snapshot.alerts), 1)

    def test_threshold_boundary_is_not_firing(self):
        self.assertEqual(normalize_prometheus_alert(raw(value=0.2)).outcome, "invalid")

    def test_boolean_signal_cannot_be_arbitrary_number(self):
        self.assertEqual(
            normalize_prometheus_alert(raw("ServiceDown", value=-1)).outcome, "invalid"
        )


class IdentityCorrelationTests(unittest.TestCase):
    def test_identity_ignores_value_threshold_severity_and_times(self):
        first = event()
        other = first.model_copy(
            update={
                "value": 0.9,
                "threshold": 0.3,
                "severity": "warning",
                "starts_at": NOW + timedelta(hours=1),
            }
        )
        self.assertEqual(
            generate_alert_fingerprint(first), generate_alert_fingerprint(other)
        )

    def test_distinct_rules_and_services(self):
        self.assertNotEqual(
            generate_alert_fingerprint(event()),
            generate_alert_fingerprint(event("HighHttpLatency")),
        )
        self.assertNotEqual(
            generate_alert_fingerprint(event()),
            generate_alert_fingerprint(event(service="inventory-service")),
        )

    def test_instance_separation_for_state_signals(self):
        first = event("ServiceDown")
        other = first.model_copy(
            update={
                "labels": {
                    **first.labels,
                    "instance": "other:8001",
                }
            }
        )
        self.assertNotEqual(
            generate_alert_fingerprint(first), generate_alert_fingerprint(other)
        )

    def test_numeric_identity_is_explicitly_service_aggregated(self):
        first = event()
        other = first.model_copy(
            update={
                "labels": {
                    **first.labels,
                    "instance": "other:8001",
                }
            }
        )
        self.assertEqual(
            generate_alert_fingerprint(first), generate_alert_fingerprint(other)
        )

    def test_dependency_relation_is_candidate_only(self):
        alerts = [event(), event("DatabaseUnavailable"), event("HighHttpLatency")]
        related = correlate_alerts(alerts)
        self.assertTrue(all(len(items) == 2 for items in related.values()))
        for items in related.values():
            self.assertTrue(
                all(
                    item["relationship"] == "candidate"
                    and not item["shared_root_cause_confirmed"]
                    for item in items
                )
            )

    def test_simultaneous_unrelated_services_have_no_relation(self):
        related = correlate_alerts([event(), event(service="inventory-service")])
        self.assertTrue(all(not items for items in related.values()))

    def test_outside_window_or_unknown_start_has_no_relation(self):
        first = event()
        for stamp in [None, NOW + timedelta(seconds=301)]:
            second = event("HighHttpLatency").model_copy(update={"starts_at": stamp})
            self.assertTrue(
                all(not items for items in correlate_alerts([first, second]).values())
            )


class StoreMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.store = IncidentStore(Path(temp) / "incidents.json")
        self.client = SimpleNamespace(
            get_alerts=Mock(return_value=envelope()),
            get_rules=Mock(return_value={}),
            query=Mock(return_value={}),
        )
        self.agent = SimpleNamespace(
            investigate=Mock(return_value=completed()), poll_tool_cache=None
        )
        self.monitor = IncidentMonitor(self.client, self.agent, self.store, 0)

    def poll(self, *entries):
        self.client.get_alerts.return_value = envelope(*entries)
        return self.monitor.process_alerts_once()

    def test_five_alerts_remain_independent(self):
        self.poll(*(raw(name) for name in ALERT_POLICIES))
        self.assertEqual(len(self.store.load()["incidents"]), 5)
        self.assertEqual(self.agent.investigate.call_count, 5)

    def test_duplicate_identity_is_investigated_once(self):
        self.poll(raw(), raw())
        self.poll(raw())
        self.assertEqual(len(self.store.load()["incidents"]), 1)
        self.agent.investigate.assert_called_once()

    def test_invalid_alert_does_not_clear_previous_incident(self):
        self.poll(raw())
        self.poll({"state": "firing"})
        self.assertEqual(len(self.store.active_incidents()), 1)

    def test_future_normalizer_can_supply_unified_events(self):
        from alert_parser import AlertSnapshot

        self.monitor.normalizer = Mock(
            return_value=AlertSnapshot(alerts=[event()], complete=False)
        )
        self.client.get_alerts.return_value = {"future_private_format": True}
        self.monitor.process_alerts_once()
        self.assertEqual(len(self.store.active_incidents()), 1)

    def test_empty_snapshot_without_metric_data_keeps_incident(self):
        self.poll(raw())
        self.poll()
        self.assertEqual(len(self.store.active_incidents()), 1)

    def test_pending_does_not_clear(self):
        self.poll(raw())
        self.poll(raw(state="pending"))
        self.assertEqual(self.store.active_incidents()[0].alert_state, "pending")

    def test_relations_enter_saved_context(self):
        self.poll(raw(), raw("DatabaseUnavailable"))
        self.assertTrue(
            all(incident.related_alerts for incident in self.store.active_incidents())
        )

    def poll_with_shared_readonly_tool(self):
        from agent.agent import Agent
        from prompts.agent_prompt import agent_prompt
        from tests.test_incident_regression import model_response, tool_call
        from tools.tool_registry import tool_registry

        sdk = SimpleNamespace(
            responses=SimpleNamespace(
                create=Mock(
                    side_effect=[
                        model_response(answer="", calls=[tool_call()]),
                        model_response(),
                        model_response(answer="", calls=[tool_call()]),
                        model_response(),
                    ],
                ),
            ),
        )
        self.monitor.agent = Agent(sdk, agent_prompt)
        probe = Mock(
            return_value={
                "status": "ok",
                "service": "order-service",
                "metric": "http_5xx_rate",
                "value": 0.8,
            },
        )
        with patch.dict(tool_registry["query_metrics"], {"func": probe}):
            self.poll(raw(), raw("HighHttpLatency"))
        return probe

    def test_related_incidents_share_one_observation_and_keep_evidence(self):
        probe = self.poll_with_shared_readonly_tool()
        incidents = self.store.active_incidents()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(incidents), 2)
        self.assertTrue(all(len(incident.evidence) == 1 for incident in incidents))
        self.assertEqual(
            sorted(incident.investigations[0].tool_calls for incident in incidents),
            [0, 1],
        )
        self.assertTrue(incidents[1].investigations[0].tool_trace[0]["from_cache"])
        self.assertIsNone(self.monitor.agent.poll_tool_cache)

    def test_long_poll_discards_old_shared_observation(self):
        with patch(
            "monitor.incident_monitor.time.monotonic",
            side_effect=[0, 0, 31, 32],
        ):
            probe = self.poll_with_shared_readonly_tool()
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(
            all(
                not incident.investigations[0].tool_trace[0]["from_cache"]
                for incident in self.store.active_incidents()
            )
        )

    def test_failure_retains_evidence_and_has_retry_delay(self):
        def fail(incident):
            incident.evidence.append(
                Evidence(source="probe", summary="real probe", detail="{}")
            )
            raise RuntimeError("external failure")

        self.agent.investigate.side_effect = fail
        self.poll(raw())
        self.poll(raw())
        self.agent.investigate.assert_called_once()
        saved = self.store.active_incidents()[0]
        self.assertEqual(len(saved.evidence), 1)
        saved.last_attempted_at -= timedelta(seconds=61)
        self.store.save_incident(saved.fingerprint, saved)
        self.poll(raw())
        self.assertEqual(self.agent.investigate.call_count, 2)

    def test_api_balance_error_stops_other_model_attempts(self):
        class BalanceError(Exception):
            status_code = 402

        self.agent.investigate.side_effect = BalanceError("not retained")
        with self.assertLogs("agent", level="ERROR"):
            self.poll(raw(), raw(service="inventory-service"))
        self.agent.investigate.assert_called_once()
        self.assertEqual(self.monitor.model_blocked_reason, "model_api_http_402")
        self.assertTrue(
            all(
                item.investigation_suspended_reason == "model_api_http_402"
                for item in self.store.active_incidents()
            )
        )

    def test_operator_resume_preserves_alert_and_evidence(self):
        self.poll(raw())
        item = self.store.active_incidents()[0]
        item.consecutive_investigation_failures = 3
        item.investigation_suspended_reason = "model_api_http_402"
        self.store.save_incident(item.fingerprint, item)
        self.store.resume_investigation(item.incident_id)
        resumed = self.store.active_incidents()[0]
        self.assertEqual(resumed.alert_state, "firing")
        self.assertEqual(resumed.consecutive_investigation_failures, 0)
        self.assertIsNone(resumed.investigation_suspended_reason)

    def test_legacy_v2_rekeys_without_losing_id_or_history(self):
        modern = event()
        old = AlertEvent(
            service=modern.service,
            severity=modern.severity,
            metric=modern.metric,
            value=0.8,
            threshold=0.2,
        )
        fingerprint = legacy_fingerprint(old)
        incident = create_incident_from_alert(old, fingerprint)
        incident.evidence.append(Evidence(source="old", summary="old", detail="{}"))
        self.store.save_incident(fingerprint, incident)
        self.poll(raw())
        saved = self.store.active_incidents()[0]
        self.assertEqual(saved.incident_id, incident.incident_id)
        self.assertEqual(saved.evidence[0].source, "old")
        self.assertEqual(saved.fingerprint, generate_alert_fingerprint(modern))
        self.assertNotIn(fingerprint, self.store.load()["active_by_fingerprint"])

    def test_legacy_v1_load_preserves_every_record(self):
        old = event()
        incident = create_incident_from_alert(old, "legacy-id")
        resolved = incident.model_dump(mode="json")
        resolved.update(
            {
                "incident_id": "inc-legacy-resolved",
                "status": "resolved",
            },
        )
        resolved.pop("alert_state")
        self.store.file_path.write_text(
            json.dumps(
                {
                    "legacy-id": incident.model_dump(mode="json"),
                    "resolved-id": resolved,
                },
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            self.store.get_active_incident("legacy-id").incident_id,
            incident.incident_id,
        )
        self.assertEqual(len(self.store.load()["incidents"]), 2)
        self.assertEqual(len(self.store.active_incidents()), 1)
        historical = Incident.model_validate(resolved)
        self.assertEqual(historical.alert_state, "cleared")
        self.assertIsNone(historical.recovery_verified_at)

    def test_cleared_is_not_business_verified(self):
        self.poll(raw())
        saved = self.store.active_incidents()[0]
        self.store.resolve_inactive_incidents(set(), {saved.fingerprint})
        row = self.store.load()["incidents"][saved.incident_id]
        self.assertEqual(row["alert_state"], "cleared")
        self.assertIsNone(row["recovery_verified_at"])
        self.store.record_recovery_verification(
            saved.incident_id,
            [Evidence(source="business_probe", summary="200", detail="{}")],
            NOW,
        )
        self.assertIsNotNone(
            self.store.load()["incidents"][saved.incident_id]["recovery_verified_at"]
        )

    def test_unvalidated_nan_cannot_pollute_store(self):
        incident = create_incident_from_alert(event(), "fp")
        incident.alerts[0].value = float("nan")
        with self.assertRaises(ValidationError):
            self.store.save_incident("fp", incident)
        self.assertFalse(self.store.load()["incidents"])


class ReinvestigationTests(unittest.TestCase):
    def seed(self, name):
        alert = event(name)
        incident = create_incident_from_alert(alert, generate_alert_fingerprint(alert))
        incident.last_investigated_at = datetime.now(timezone.utc)
        incident.last_investigated_value = alert.value
        incident.last_investigated_state = "firing"
        return incident, alert

    def test_http_absolute_worsening(self):
        incident, alert = self.seed("HighHttp5xxRate")
        incident.last_investigated_value = 0.5
        self.assertTrue(
            has_alert_worsened(incident, alert.model_copy(update={"value": 0.7}))
        )

    def test_latency_absolute_and_relative_worsening(self):
        incident, alert = self.seed("HighHttpLatency")
        self.assertTrue(
            has_alert_worsened(incident, alert.model_copy(update={"value": 1.3}))
        )
        self.assertFalse(
            has_alert_worsened(incident, alert.model_copy(update={"value": 0.81}))
        )

    def test_state_signals_do_not_use_http_delta(self):
        for name in ["ServiceDown", "DatabaseUnavailable", "PostgresExporterDown"]:
            incident, alert = self.seed(name)
            self.assertFalse(has_alert_worsened(incident, alert))
            self.assertFalse(should_reinvestigate(incident, alert))

    def test_state_change_can_reinvestigate(self):
        incident, alert = self.seed("ServiceDown")
        incident.last_investigated_state = "pending"
        self.assertTrue(should_reinvestigate(incident, alert))

    def test_cooldown_is_finite_for_state_signals(self):
        incident, alert = self.seed("ServiceDown")
        incident.last_investigated_at -= timedelta(seconds=301)
        self.assertTrue(should_reinvestigate(incident, alert))

    def test_modern_numeric_worsening_obeys_minimum_interval(self):
        incident, alert = self.seed("HighHttpLatency")
        incident.last_attempted_at = datetime.now(timezone.utc)
        self.assertFalse(
            should_reinvestigate(incident, alert.model_copy(update={"value": 5.0}))
        )

    def test_long_successful_investigation_has_interval_after_completion(self):
        incident, alert = self.seed("HighHttp5xxRate")
        incident.last_attempted_at = incident.last_investigated_at - timedelta(
            seconds=40
        )
        incident.last_investigated_value = 0.3
        self.assertFalse(
            should_reinvestigate(incident, alert.model_copy(update={"value": 1.0}))
        )

    def test_repeated_external_failures_stop_unbounded_llm_retries(self):
        incident, alert = self.seed("HighHttp5xxRate")
        incident.last_investigated_at = None
        incident.consecutive_investigation_failures = 3
        self.assertFalse(should_reinvestigate(incident, alert))


class RecoveryTests(unittest.TestCase):
    def test_absent_pg_up_is_distinct_from_zero(self):
        client = SimpleNamespace(
            query=Mock(
                return_value={
                    "status": "success",
                    "data": {"result": []},
                }
            )
        )
        self.assertIsNone(present_value(client, "pg_up"))
        client.query.return_value["data"]["result"] = [
            {"value": [datetime.now(timezone.utc).timestamp(), "0"]}
        ]
        self.assertEqual(present_value(client, "pg_up"), 0)

    def test_stale_nan_and_multiple_samples_are_unusable(self):
        for samples in [
            [{"value": [0, "1"]}],
            [{"value": [datetime.now(timezone.utc).timestamp(), "NaN"]}],
            [{"value": [datetime.now(timezone.utc).timestamp(), True]}],
            [{"value": [True, "1"]}],
            [{"value": [datetime.now(timezone.utc).timestamp(), "1"]}] * 2,
        ]:
            client = SimpleNamespace(
                query=Mock(
                    return_value={
                        "status": "success",
                        "data": {"result": samples},
                    }
                )
            )
            self.assertIsNone(present_value(client, "up"))

    def test_pg_up_missing_or_exporter_down_cannot_clear_database_alert(self):
        alert = event("DatabaseUnavailable")
        rule = {
            alert.alertname: {"annotations": raw("DatabaseUnavailable")["annotations"]}
        }
        client = SimpleNamespace(
            query=Mock(
                return_value={
                    "status": "success",
                    "data": {"result": []},
                }
            )
        )
        self.assertFalse(can_clear(alert, client, rule))
        client.query.return_value["data"]["result"] = [
            {"value": [datetime.now(timezone.utc).timestamp(), "0"]}
        ]
        self.assertFalse(can_clear(alert, client, rule))

    def test_valid_state_recovery_clears_signal(self):
        alert = event("ServiceDown")
        client = SimpleNamespace(
            query=Mock(
                return_value={
                    "status": "success",
                    "data": {
                        "result": [
                            {"value": [datetime.now(timezone.utc).timestamp(), "1"]}
                        ]
                    },
                }
            )
        )
        self.assertTrue(
            can_clear(
                alert,
                client,
                {alert.alertname: {"annotations": raw("ServiceDown")["annotations"]}},
            )
        )

    def test_missing_rules_health_blocks_clear(self):
        self.assertEqual(
            rules_are_current(
                SimpleNamespace(
                    get_rules=Mock(
                        return_value={
                            "status": "success",
                            "data": {"groups": []},
                        }
                    )
                )
            ),
            {},
        )


class MetricToolTests(unittest.TestCase):
    def test_missing_pg_up_does_not_become_zero(self):
        from tools.metrics import query_metrics

        with patch(
            "tools.metrics.prometheus_client.query",
            return_value={
                "status": "success",
                "data": {"result": []},
            },
        ):
            result = query_metrics("order-db", "postgres_up")
        self.assertEqual(result["status"], "no_data")
        self.assertNotIn("value", result)

    def test_new_metric_semantic_domains_are_validated(self):
        from tools.metrics import query_metrics

        for service, metric, value in [
            ("order-db", "exporter_scrape_up", "2"),
            ("order-db", "exporter_scrape_up", True),
            ("inventory-service", "http_5xx_rate", False),
            ("inventory-service", "http_latency_p95_seconds", "-1"),
            ("inventory-service", "http_5xx_rate", "1.5"),
        ]:
            with patch(
                "tools.metrics.prometheus_client.query",
                return_value={
                    "status": "success",
                    "data": {"result": [{"value": [0, value]}]},
                },
            ):
                self.assertEqual(query_metrics(service, metric)["status"], "no_data")

    def test_scrape_failure_is_real_target_evidence(self):
        from tools.metrics import check_scrape_target

        with patch(
            "tools.metrics.prometheus_client.get_targets",
            return_value={
                "status": "success",
                "data": {
                    "activeTargets": [
                        {
                            "labels": {"service": "inventory-service"},
                            "health": "down",
                            "lastError": "connection refused",
                        }
                    ]
                },
            },
        ):
            result = check_scrape_target("inventory-service")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["targets"][0]["health"], "down")

    def test_invalid_target_payloads_return_no_data(self):
        from tools.metrics import check_scrape_target

        for payload in [
            [],
            {},
            {
                "status": "success",
                "data": {"activeTargets": [None]},
            },
        ]:
            with patch(
                "tools.metrics.prometheus_client.get_targets", return_value=payload
            ):
                self.assertEqual(
                    check_scrape_target("inventory-service")["status"], "no_data"
                )


class RemediationSafetyTests(unittest.TestCase):
    def test_five_alert_signals_alone_never_authorize_action(self):
        planner = RemediationPlanner(Mock())
        for name in ALERT_POLICIES:
            incident = create_incident_from_alert(event(name), "fp")
            self.assertIsNone(planner.plan(incident, completed()))

    def test_inventory_has_no_action_permission(self):
        incident = create_incident_from_alert(event(service="inventory-service"), "fp")
        self.assertIsNone(RemediationPlanner(Mock()).plan(incident, completed()))

    def test_related_incidents_deduplicate_same_process_proposal(self):
        from tests.test_remediation import proposal

        temp = self.enterContext(tempfile.TemporaryDirectory())
        store = RemediationStore(Path(temp) / "remediation.json")
        first = proposal()
        second = proposal(incident_id="inc-related")
        self.assertEqual(
            store.create(first).proposal_id, store.create(second).proposal_id
        )
        self.assertEqual(len(store.list()), 1)


if __name__ == "__main__":
    unittest.main()
