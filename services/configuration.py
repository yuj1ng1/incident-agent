"""The single source of service definitions. Secrets are environment references."""

SERVICES = [
    {
        "name": "order-service",
        "type": "http_service",
        "health": {
            "provider": "http",
            "base_url": "http://localhost:8001",
        },
        "logs": {
            "provider": "loki",
            "labels": {"service": "order-service"},
        },
        "metrics": {
            "provider": "prometheus",
            "profile": "http_service",
        },
        "runtime": {
            "provider": "http_version",
            "base_url": "http://localhost:8001",
            "allowed_flags": ("simulate_code_regression", "simulate_slow_request"),
        },
        "source": {
            "provider": "github",
            "repo": "yuj1ng1/prometheus-demo",
            "branch": "codex/postgres-monitoring",
            "investigation_paths": ("app.py", "http_observability.py"),
        },
        "dependencies": ("order-db",),
        # /order was reviewed: SELECT only, no business-data mutation.
        # Normal access logs/metrics are expected observational effects.
        "recovery": {
            "provider": "http_business",
            "target_id": "order_read_query",
            "base_url": "http://127.0.0.1:8001",
            "endpoint": "/order",
            "side_effect_free": True,
            "json_equals": {"service": "order-service", "dependency": "order-db"},
            "required_json_objects": ("order",),
        },
        "remediation": ("disable_demo_regression",),
    },
    {
        "name": "order-db",
        "type": "postgresql",
        "health": {
            "provider": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "orders",
            "user": "demo",
            "password_env": "ORDER_DB_PASSWORD",
        },
        "metrics": {
            "provider": "prometheus",
            "profile": "postgresql",
            "job": "postgres",
            "database": "orders",
        },
    },
    {
        "name": "inventory-service",
        "type": "http_service",
        "health": {
            "provider": "http",
            "base_url": "http://localhost:8002",
        },
        "logs": {
            "provider": "loki",
            "labels": {"service": "inventory-service"},
        },
        "metrics": {
            "provider": "prometheus",
            "profile": "http_service",
        },
        "runtime": {
            "provider": "http_version",
            "base_url": "http://localhost:8002",
            "allowed_flags": ("simulate_inventory_error", "simulate_slow_request"),
        },
        "source": {
            "provider": "github",
            "repo": "yuj1ng1/prometheus-demo",
            "branch": "codex/postgres-monitoring",
            "investigation_paths": ("inventory_app.py", "http_observability.py"),
        },
    },
]
