import requests

class ServiceClient:
    def __init__(self,base_url):
        self.base_url=base_url

    def health(self):
        response=requests.get(
            f"{self.base_url}/health",
            timeout=5
        )
        try:
            body=response.json()
        except ValueError:
            body={
                "message":response.text
            }
        return {
            "http_status":response.status_code,
            "body":body
        }