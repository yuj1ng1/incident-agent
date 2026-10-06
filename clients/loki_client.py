import requests
class LokiClient:
    def __init__(self,base_url):
        self.base_url=base_url

    def query_range(self,logql,limit=50):
        response=requests.get(
            f"{self.base_url}/loki/api/v1/query_range",
            params={
                "query":logql,
                "limit":limit,
                "diection":"backward"
            },
            timeout=5
        )
        response.raise_for_status()
        return response.json()