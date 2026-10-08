import requests

class PrometheusClient:
    def __init__(self,base_url):
        self.base_url=base_url

    def query(self,promql):#查询指标
        response=requests.get(
            f"{self.base_url}/api/v1/query",
            params={
                "query":promql
            },
            timeout=5
        )
        response.raise_for_status()

        return response.json()

    def get_alerts(self):#查询当前告警
        response=requests.get(
            f"{self.base_url}/api/v1/alerts",
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def get_rules(self):
        response = requests.get(
            f"{self.base_url}/api/v1/rules",
            params={"type": "alert"},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()

    def get_targets(self):
        response = requests.get(
            f"{self.base_url}/api/v1/targets",
            params={"state": "active"},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
