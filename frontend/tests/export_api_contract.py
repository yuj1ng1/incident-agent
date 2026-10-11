"""Export only the live public OpenAPI responses for frontend contract tests.

Run from the repository root with the project's Python environment. Importing
the API creates no store, scheduler or model client. No business data is read.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_api.app import create_app


def references(value):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"].rsplit("/", 1)[1]
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def public_contract():
    app = create_app()
    openapi = app.openapi()
    responses = {
        path: methods["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        for path, methods in openapi["paths"].items()
        if path.startswith("/api/")
    }
    schemas = {}
    pending = set(references(responses))
    while pending:
        name = pending.pop()
        schemas[name] = openapi["components"]["schemas"][name]
        pending.update(set(references(schemas[name])) - schemas.keys())
    serialization = {
        route.path: {
            "exclude_unset": route.response_model_exclude_unset,
            "exclude_defaults": route.response_model_exclude_defaults,
            "exclude_none": route.response_model_exclude_none,
        }
        for route in app.routes
        if route.path in responses
    }
    return {"responses": responses, "schemas": schemas, "serialization": serialization}


if __name__ == "__main__":
    destination = Path(__file__).with_name("api-contract.json")
    destination.write_text(json.dumps(public_contract(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Exported public OpenAPI contract (no runtime data)")
