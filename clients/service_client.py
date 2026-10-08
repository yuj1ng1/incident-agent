import requests

class ServiceClient:
    def __init__(self,base_url):
        self.base_url=base_url

    def health(self, endpoint="/health"):
        response = requests.get(
            f"{self.base_url.rstrip('/')}{endpoint}",
            timeout=5,
        )
        try:
            body = response.json()
        except ValueError:
            body = {
                "message": response.text,
            }
        return {
            "http_status": response.status_code,
            "body": body,
        }

    def version(self, endpoint="/version"):
        response = requests.get(
            f"{self.base_url.rstrip('/')}{endpoint}",
            timeout=5,
        )
        return {
            "http_status": response.status_code,
            "body": response.json() if response.status_code == 200 else None,
        }
