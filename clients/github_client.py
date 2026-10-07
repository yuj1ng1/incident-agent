import requests
import os
from urllib.parse import quote

class GithubClient:
    def __init__(self,token=None):
        self.base_url="https://api.github.com"
        self.token=token

    def get_recent_commits(self,repo,branch,limit=5):
        headers=self.headers()

        response=requests.get(
            f"{self.base_url}/repos/{repo}/commits",
            headers=headers,
            params={
                "sha":branch,
                "per_page":limit
            },
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def headers(self,accept="application/vnd.github+json"):
        headers={"Accept":accept}
        # 延迟读取，避免 tools 在 load_dotenv 前导入时永久缓存空 Token。
        token=self.token or os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"]=f"Bearer {token}"
        return headers

    def get_commit(self,repo,sha,file_limit=15):
        response=requests.get(
            f"{self.base_url}/repos/{repo}/commits/{quote(sha,safe='')}",
            headers=self.headers(),params={"per_page":file_limit},timeout=5
        )
        response.raise_for_status()
        body=response.json()
        if isinstance(body,dict):
            body["_files_paginated"]="next" in response.links
        return body

    def get_file_content(self,repo,path,ref):
        response=requests.get(
            f"{self.base_url}/repos/{repo}/contents/{quote(path,safe='/')}",
            headers=self.headers(),params={"ref":ref},timeout=5
        )
        response.raise_for_status()
        return response.json()

    def get_file_prefix(self,repo,path,ref,byte_limit):
        # >1MB 文件的 Contents JSON 不包含 base64，改读有上限的 raw 前缀。
        with requests.get(
            f"{self.base_url}/repos/{repo}/contents/{quote(path,safe='/')}",
            headers=self.headers("application/vnd.github.raw+json"),
            params={"ref":ref},timeout=5,stream=True
        ) as response:
            response.raise_for_status()
            data=bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                data.extend(chunk[:max(0,byte_limit-len(data))])
                if len(data)>=byte_limit:
                    break
        return bytes(data)
