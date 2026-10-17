"""Typed references for the isolated preflight, without I/O or Runtime changes.

Hashes detect changes and bind a catalog to its supplied snapshot. They do not
authenticate an arbitrary caller's snapshot or establish semantic claim support.
"""
from copy import deepcopy
import hashlib
import json
import re


INCIDENT_CONTEXT_PREFIX = (
    "\n    统一调查上下文（Python 校验；Policy 提示和候选关联不是根因）：\n    "
)
CONTEXT_TYPES = frozenset({
    "alert_event",
    "capability_context",
    "identity_context",
    "evidence_collection_scope",
})
_SOURCE_LOCATIONS = {
    "alert_event": "runtime_input/incident_context/alert_event",
    "capability_context": "runtime_input/incident_context/capabilities",
    "evidence_collection_scope": "source_snapshot/records",
}
_PERMISSIONS = {
    "alert_event": {
        "may_support": ["provided_alert_event_metadata"],
        "cannot_establish": [
            "root_cause", "current_service_health", "tool_execution",
        ],
    },
    "capability_context": {
        "may_support": ["declared_service_capabilities_and_dependencies"],
        "cannot_establish": [
            "tool_execution", "backend_reachability", "dependency_health",
            "current_service_health",
        ],
    },
    "identity_context": {
        "may_support": ["persisted_identity_metadata_and_historical_association"],
        "cannot_establish": [
            "current_running_version", "current_service_health",
            "unverified_instance_as_verified_deployment_identity",
        ],
    },
    "evidence_collection_scope": {
        "may_support": [
            "membership_and_absence_within_listed_pre_request_checkpoint_records",
            "runtime_observation_summary_coverage_of_that_snapshot",
        ],
        "cannot_establish": [
            "all_evidence_in_the_real_world", "absence_of_logs_or_faults",
            "tools_executed_during_this_preflight_run",
            "current_service_health", "unbounded_universal_claims",
        ],
    },
}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def json_sha256(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("preflight_reference_duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("preflight_reference_nonfinite_json_number")


def _parse_json(text):
    value = json.loads(
        text,
        object_pairs_hook=_json_object,
        parse_constant=_invalid_constant,
    )
    # json.loads otherwise accepts overflowing finite literals such as 1e999.
    json_sha256(value)
    return value


def _runtime_context(runtime_input):
    """Read the two fixed positions in Agent/Worker's evidence-rebuild message.

    Ordinary user JSON, nested JSON and marker text elsewhere are never searched
    for metadata. This entry accepts evidence_rebuild, not complete_chain.
    """
    if not isinstance(runtime_input, list):
        raise ValueError("preflight_reference_runtime_input_invalid")
    candidates = []
    for item in runtime_input:
        if not isinstance(item, dict):
            raise ValueError("preflight_reference_runtime_message_invalid")
        content = item.get("content")
        if (
            item.get("role") != "user"
            or not isinstance(content, str)
            or not content.startswith(INCIDENT_CONTEXT_PREFIX)
        ):
            continue
        try:
            metadata = _parse_json(
                content[len(INCIDENT_CONTEXT_PREFIX):].split("\n", 1)[0],
            )
            rebuilt = _parse_json(content.rsplit("\n", 1)[-1])
        except (ValueError, TypeError, RecursionError):
            raise ValueError("preflight_reference_runtime_context_invalid") from None
        metadata_types = {
            "alert_event": (dict, type(None)),
            "policy": (dict, type(None)),
            "capabilities": (dict, type(None)),
            "related_alerts": (list,),
        }
        if not isinstance(metadata, dict) or any(
            key not in metadata_types
            or not isinstance(value, metadata_types[key])
            for key, value in metadata.items()
        ):
            raise ValueError("preflight_reference_incident_context_invalid")
        if (
            not isinstance(rebuilt, dict)
            or not {
                "recovery", "historical_observations",
                "failed_or_missing_observations", "metric_analysis",
                "context_collected_at",
            }.issubset(rebuilt)
            or not isinstance(rebuilt["recovery"], str)
            or not isinstance(rebuilt["historical_observations"], list)
            or not isinstance(rebuilt["failed_or_missing_observations"], list)
            or not isinstance(rebuilt["metric_analysis"], (dict, type(None)))
            or not isinstance(rebuilt["context_collected_at"], str)
        ):
            raise ValueError("preflight_reference_evidence_rebuild_invalid")
        candidates.append((metadata, rebuilt))
    if len(candidates) != 1:
        raise ValueError("preflight_requires_one_runtime_evidence_rebuild")
    return candidates[0]


def _observation_ids(values):
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value or value.startswith("context:")
        for value in values
    ) or len(set(values)) != len(values):
        raise ValueError("preflight_reference_observation_ids_invalid")
    return values


def _source_id(binding, source):
    digest = json_sha256({
        "binding": binding,
        "source": {
            key: value for key, value in source.items() if key != "reference_id"
        },
    })
    return f"context:{source['source_type']}:{digest}"


def build_reference_context(
    records,
    runtime_input,
    source_snapshot,
):
    """Build references from one persisted snapshot and actual Runtime input.

    The caller must obtain source_snapshot from a consistent read transaction.
    Its records are the complete checkpoint record list before this request;
    they are not evidence of new tool executions during this preflight run.
    """
    if (
        not isinstance(records, list)
        or not isinstance(source_snapshot, dict)
        or json_sha256(records) != json_sha256(source_snapshot.get("records"))
        or any(
            not isinstance(source_snapshot.get(key), str)
            or not source_snapshot[key]
            for key in ("session_id", "checkpoint_id")
        )
    ):
        raise ValueError("preflight_reference_snapshot_mismatch")
    ids = _observation_ids([
        item.get("observation_id") if isinstance(item, dict) else None
        for item in records
    ])
    if any(
        not isinstance(item.get("name"), str)
        or not item["name"]
        or not isinstance(item.get("arguments"), dict)
        or not isinstance(item.get("result"), dict)
        or not isinstance(item["result"].get("status"), str)
        or (item.get("identity") is not None
            and not isinstance(item["identity"], dict))
        for item in records
    ) or (
        source_snapshot.get("identity") is not None
        and not isinstance(source_snapshot["identity"], dict)
    ):
        raise ValueError("preflight_reference_record_invalid")
    metadata, rebuilt = _runtime_context(runtime_input)
    observations = (
        rebuilt["historical_observations"]
        + rebuilt["failed_or_missing_observations"]
    )
    visible_ids = _observation_ids([
        item.get("observation_id") if isinstance(item, dict) else None
        for item in observations
    ])
    by_id = {item["observation_id"]: item for item in records}
    for observation in observations:
        record = by_id.get(observation["observation_id"])
        if record is None or any(
            key not in observation or observation[key] != record.get(key)
            for key in ("name", "arguments", "collected_at")
        ) or observation.get("status") != record["result"]["status"]:
            raise ValueError("preflight_reference_runtime_source_mismatch")
    binding = {
        "session_id": source_snapshot["session_id"],
        "checkpoint_id": source_snapshot["checkpoint_id"],
        "records_sha256": json_sha256(records),
        "runtime_input_sha256": json_sha256(runtime_input),
    }
    sources = []

    def append_source(source_type, location, value):
        source = {
            "source_type": source_type,
            "source_location": location,
            "value": deepcopy(value),
            "evidence_permissions": deepcopy(_PERMISSIONS[source_type]),
        }
        sources.append({"reference_id": _source_id(binding, source), **source})

    for key, source_type in (
        ("alert_event", "alert_event"),
        ("capabilities", "capability_context"),
    ):
        if isinstance(metadata.get(key), dict):
            append_source(source_type, _SOURCE_LOCATIONS[source_type], metadata[key])
    for index, record in enumerate(records):
        if record.get("identity"):
            append_source(
                "identity_context",
                f"source_snapshot/records/{index}/identity",
                record["identity"],
            )
    if source_snapshot.get("identity"):
        append_source(
            "identity_context", "source_snapshot/identity", source_snapshot["identity"],
        )
    append_source(
        "evidence_collection_scope",
        _SOURCE_LOCATIONS["evidence_collection_scope"],
        {
            "scope": "pre_request_checkpoint_records_only",
            "record_coverage": "all_snapshot_records",
            "records": [{
                "observation_id": record["observation_id"],
                "name": record["name"],
                "arguments": deepcopy(record["arguments"]),
                "status": record["result"]["status"],
                "record_sha256": json_sha256(record),
            } for record in records],
            "collected_tool_names": sorted({record["name"] for record in records}),
            "runtime_observation_coverage": (
                "complete" if set(visible_ids) == set(ids) else "partial"
            ),
            "runtime_visible_observation_ids": visible_ids,
            "runtime_omitted_observation_ids": [
                source_id for source_id in ids if source_id not in visible_ids
            ],
        },
    )
    catalog = {
        "schema_version": 1,
        "binding": binding,
        "context_sources": sources,
    }
    return {**catalog, "catalog_sha256": json_sha256(catalog)}


def verify_reference_context(
    context,
    observation_ids,
    *,
    records=None,
    runtime_input=None,
    source_snapshot=None,
):
    """Reject malformed catalogs and optionally rederive their actual sources.

    With no external inputs this checks internal integrity only: a hash is not
    provenance authentication. Supply all three external inputs to rederive the
    catalog and reject internally consistent but fabricated metadata. Neither
    mode decides whether a citation supports a natural-language claim.
    """
    ids = _observation_ids(observation_ids)
    if not isinstance(context, dict) or set(context) != {
        "schema_version", "binding", "context_sources", "catalog_sha256",
    } or type(context["schema_version"]) is not int or context["schema_version"] != 1:
        raise ValueError("preflight_reference_context_invalid")
    binding = context["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "session_id", "checkpoint_id", "records_sha256", "runtime_input_sha256",
    } or any(
        not isinstance(binding[key], str) or not binding[key]
        for key in binding
    ) or any(
        not _DIGEST.fullmatch(binding[key])
        for key in ("records_sha256", "runtime_input_sha256")
    ):
        raise ValueError("preflight_reference_binding_invalid")
    sources = context["context_sources"]
    if not isinstance(sources, list):
        raise ValueError("preflight_reference_sources_invalid")
    seen_ids = set(ids)
    seen_locations = set()
    scopes = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "reference_id", "source_type", "source_location", "value",
            "evidence_permissions",
        } or not isinstance(source.get("source_type"), str):
            raise ValueError("preflight_reference_source_invalid")
        source_type = source["source_type"]
        if source_type not in CONTEXT_TYPES:
            raise ValueError("preflight_reference_type_invalid")
        location = source["source_location"]
        if not isinstance(location, str) or (
            source_type == "identity_context" and not re.fullmatch(
                r"source_snapshot/(?:identity|records/(?:0|[1-9][0-9]*)/identity)",
                location,
            )
        ) or (
            source_type != "identity_context"
            and location != _SOURCE_LOCATIONS[source_type]
        ):
            raise ValueError("preflight_reference_location_invalid")
        if (
            not isinstance(source["value"], dict)
            or source["evidence_permissions"] != _PERMISSIONS[source_type]
            or source["reference_id"] != _source_id(binding, source)
            or source["reference_id"] in seen_ids
            or location in seen_locations
        ):
            raise ValueError("preflight_reference_source_integrity_invalid")
        seen_ids.add(source["reference_id"])
        seen_locations.add(location)
        if source_type == "evidence_collection_scope":
            scopes.append(source["value"])
    if len(scopes) != 1:
        raise ValueError("preflight_reference_scope_required")
    scope = scopes[0]
    if set(scope) != {
        "scope", "record_coverage", "records", "collected_tool_names",
        "runtime_observation_coverage", "runtime_visible_observation_ids",
        "runtime_omitted_observation_ids",
    } or (
        scope["scope"] != "pre_request_checkpoint_records_only"
        or scope["record_coverage"] != "all_snapshot_records"
        or not isinstance(scope["records"], list)
    ):
        raise ValueError("preflight_reference_scope_invalid")
    for record in scope["records"]:
        if not isinstance(record, dict) or set(record) != {
            "observation_id", "name", "arguments", "status", "record_sha256",
        } or (
            not isinstance(record["name"], str) or not record["name"]
            or not isinstance(record["arguments"], dict)
            or not isinstance(record["status"], str)
            or not isinstance(record["record_sha256"], str)
            or not _DIGEST.fullmatch(record["record_sha256"])
        ):
            raise ValueError("preflight_reference_scope_record_invalid")
    scope_ids = _observation_ids([
        item["observation_id"] for item in scope["records"]
    ])
    visible = _observation_ids(scope["runtime_visible_observation_ids"])
    omitted = _observation_ids(scope["runtime_omitted_observation_ids"])
    if (
        scope_ids != ids
        or not set(visible).issubset(ids)
        or omitted != [source_id for source_id in ids if source_id not in visible]
        or scope["collected_tool_names"] != sorted({
            item["name"] for item in scope["records"]
        })
        or scope["runtime_observation_coverage"] != (
            "complete" if set(visible) == set(ids) else "partial"
        )
    ):
        raise ValueError("preflight_reference_scope_binding_invalid")
    for source in sources:
        if source["source_type"] != "identity_context":
            continue
        if not source["value"]:
            raise ValueError("preflight_reference_identity_missing")
        match = re.fullmatch(
            r"source_snapshot/records/([0-9]+)/identity",
            source["source_location"],
        )
        if match and int(match[1]) >= len(scope_ids):
            raise ValueError("preflight_reference_identity_location_invalid")
    actual_hash = json_sha256({
        key: value for key, value in context.items() if key != "catalog_sha256"
    })
    if context["catalog_sha256"] != actual_hash:
        raise ValueError("preflight_reference_catalog_hash_mismatch")
    external = (records, runtime_input, source_snapshot)
    if any(value is not None for value in external):
        if any(value is None for value in external):
            raise ValueError("preflight_reference_external_sources_incomplete")
        if context != build_reference_context(*external):
            raise ValueError("preflight_reference_external_sources_mismatch")
