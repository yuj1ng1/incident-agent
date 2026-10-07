MODEL_NAME="deepseek-flash"

MAX_AGENT_LOOPS=7
MAX_TOOL_CALLS=13
MODEL_RETRIES=3

PROMETHEUS_URL="http://localhost:9090"
LOKI_URL="http://localhost:3100"
SERVICE_URLS={
    "order-service":"http://localhost:8001"
    }
MONITORING_TARGETS={
    "order-db":{
        "type":"postgresql",
        "job":"postgres",
        "database":"orders"
    }
}

REINVESTIGATE_COOLDOWN_MINUTES=5

HTTP_5XX_RATE_INCREASE_THRESHOLD=0.2

ALERT_POLL_INTERVAL_SECONDS=30

SERVICE_DEPENDENCIES={
    "order-service":[
        {
            "name":"order-db",
            "type":"database"
        }
    ],
    "order-db":[]
}

DEPENDENCIES={
    "order-db":{
        "type":"database",
        "engine":"postgresql",
        "host":"127.0.0.1",
        "port":5432,
        "database":"orders",
        "user":"demo",
        "password":"demo"
    }
}

CODE_REPOSITORIES={
    "order-service":{
        "provider":"github",
        "repo":"yuj1ng1/prometheus-demo",
        "branch":"codex/postgres-monitoring"
    }
}