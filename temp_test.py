from clients.prometheus_client import PrometheusClient
from alert_parser import parse_prometheus_alert
from config import PROMETHEUS_URL


client=PrometheusClient(
    base_url=PROMETHEUS_URL
)

response=client.get_alerts()

raw_alerts=response.get(
    "data",{}
).get(
    "alerts",[]
)
for raw_alert in raw_alerts:
    alert=parse_prometheus_alert(raw_alert)
    if alert is not None:
        print(alert)