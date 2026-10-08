MODEL_NAME="deepseek-flash"

MAX_AGENT_LOOPS=7
MAX_TOOL_CALLS=13
MODEL_RETRIES=3

PROMETHEUS_URL="http://localhost:9090"
LOKI_URL="http://localhost:3100"
REINVESTIGATE_COOLDOWN_MINUTES=5

HTTP_5XX_RATE_INCREASE_THRESHOLD=0.2

ALERT_POLL_INTERVAL_SECONDS=30

# Legacy imports remain read-only projections of the validated registry.
from services.compatibility import LegacyView
from services.configuration import SERVICES

SERVICE_URLS = LegacyView("SERVICE_URLS")
SERVICE_DEPENDENCIES = LegacyView("SERVICE_DEPENDENCIES")
DEPENDENCIES = LegacyView("DEPENDENCIES")
MONITORING_TARGETS = LegacyView("MONITORING_TARGETS")
CODE_REPOSITORIES = LegacyView("CODE_REPOSITORIES")
