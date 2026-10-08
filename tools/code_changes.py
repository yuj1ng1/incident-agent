import base64
import binascii
import codecs
import re

import requests

from tools.tool_registry import tool
from clients.github_client import GithubClient
from services.registry import registry, CapabilityUnavailable


MAX_DIFF_FILES=15
MAX_PATCH_CHARS=6000
MAX_DIFF_CHARS=24000
MAX_SOURCE_CHARS=20000
MAX_SOURCE_BYTES=MAX_SOURCE_CHARS*4+4
github_client=GithubClient()


def repository_for(service):
    try:
        source = registry.require_capability(service, "source")
    except CapabilityUnavailable as exc:
        return None, exc.reason
    if source.provider != "github":
        return None, "unsupported_provider"
    return source.model_dump(), None


def read_error(service,repo,exc):
    result={"status":"no_data","service":service,"repository":repo}
    if isinstance(exc,requests.HTTPError) and exc.response is not None:
        code=exc.response.status_code
        reasons={404:"not_found",403:"access_denied_or_rate_limited",401:"authentication_failed",
                 422:"invalid_ref",429:"rate_limited"}
        return {**result,"reason":reasons.get(code,"github_http_error"),"http_status":code}
    if isinstance(exc,requests.RequestException):
        return {**result,"reason":"github_unreachable"}
    return {**result,"reason":"invalid_github_response"}


def valid_path(path):
    return (
        isinstance(path,str) and 0<len(path)<=500 and not path.startswith("/")
        and not any(c in path for c in "\\%?#:")
        and not any(ord(c)<32 or ord(c)==127 for c in path)
        and all(part not in ("",".","..") for part in path.split("/"))
    )


def valid_ref(ref):
    return (
        isinstance(ref,str) and 0<len(ref)<=200
        and not any(c.isspace() or ord(c)<32 for c in ref)
        and not any(c in ref for c in "\\%?#:") and ".." not in ref
    )

@tool
def get_recent_code_changes(service:str)->dict:
    """
    查询指定服务对应代码仓库最近的代码提交。

    Args:
        service: 需要查询近期代码变更的服务名称
    """

    repository,error=repository_for(service)
    if error:
        return {
            "status":"no_data",
            "service":service,
            "reason":error
        }
    if repository.get("provider")!="github":
        return {
            "status":"no_data",
            "service":service,
            "message":"暂不支持该仓库类型"
        }
    repo=repository.get("repo")
    branch=repository.get("branch")

    if not repo or not branch:
        return {
            "status":"no_data",
            "service":service,
            "message":"代码仓库配置不完整"
        }
    try:
        commits=github_client.get_recent_commits(repo=repo,branch=branch,limit=5)
    except (requests.RequestException,ValueError) as exc:
        return read_error(service,repo,exc)
    if not isinstance(commits,list):
        return {"status":"no_data","service":service,"reason":"invalid_github_response"}
    if not commits:
        return {
            "status":"no_data",
            "service":service,
            "repository":repo,
            "branch":branch
        }
    changes=[]
    for item in commits[:5]:
        if not isinstance(item,dict) or not isinstance(item.get("commit"),dict):
            return {"status":"no_data","service":service,"reason":"invalid_github_response"}
        commit=item.get("commit",{})
        author=commit.get("author") or {}
        committer=commit.get("committer") or {}

        changes.append({
            "sha":item.get("sha"),
            "message":str(commit.get("message", ""))[:2000],
            "author":author.get("name"),
            "committed_at":committer.get("date")
        })
    return {
        "status":"ok",
        "service":service,
        "repository":repo,
        "branch":branch,
        "changes":changes
    }


@tool
def get_commit_diff(service:str,commit_sha:str)->dict:
    """只读查询配置仓库指定 commit 的有界 diff；不证明该提交已部署。

    Args:
        service: ServiceRegistry 中已配置的服务名称，不能传任意仓库
        commit_sha: 值得调查的 Git commit SHA，7 到 40 位十六进制
    """
    repository,error=repository_for(service)
    if error:
        return {"status":"no_data","service":service,"reason":error}
    repo=repository["repo"]
    if not isinstance(commit_sha,str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}",commit_sha):
        return {"status":"no_data","service":service,"reason":"invalid_commit_sha"}
    try:
        body=github_client.get_commit(repo,commit_sha,file_limit=MAX_DIFF_FILES)
    except (requests.RequestException,ValueError) as exc:
        return read_error(service,repo,exc)
    if (not isinstance(body,dict) or not isinstance(body.get("commit"),dict)
            or not isinstance(body.get("files"),list)
            or not isinstance(body.get("sha"),str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}",body["sha"])
            or not body["sha"].lower().startswith(commit_sha.lower())):
        return {"status":"no_data","service":service,"reason":"invalid_github_response"}
    files=[]
    remaining=MAX_DIFF_CHARS
    files_truncated=bool(body.get("_files_paginated")) or len(body["files"])>MAX_DIFF_FILES
    truncated=files_truncated
    for item in body["files"][:MAX_DIFF_FILES]:
        if not isinstance(item,dict) or not isinstance(item.get("filename"),str):
            return {"status":"no_data","service":service,"reason":"invalid_github_response"}
        patch=item.get("patch")
        available=isinstance(patch,str)
        returned=patch[:min(MAX_PATCH_CHARS,remaining)] if available else None
        cut=not available or len(returned)<len(patch)
        remaining-=len(returned or "")
        truncated=truncated or cut
        files.append({
            "filename":item["filename"],"status":item.get("status"),
            "additions":item.get("additions"),"deletions":item.get("deletions"),
            "changes":item.get("changes"),"patch":returned,
            "patch_available":available,"truncated":cut,
            "previous_filename":item.get("previous_filename")
        })
    commit=body["commit"]
    message=str(commit.get("message", ""))
    return {
        "status":"ok","service":service,"repository":repo,
        "commit":{"sha":body["sha"],"message":message[:2000],
                  "message_truncated":len(message)>2000,
                  "committed_at":(commit.get("committer") or {}).get("date")},
        "files":files,"truncated":truncated,"files_truncated":files_truncated,
        "limits":{"files":MAX_DIFF_FILES,"patch_chars":MAX_PATCH_CHARS,
                  "total_patch_chars":MAX_DIFF_CHARS}
    }


@tool
def get_source_file(service:str,path:str,ref:str)->dict:
    """只读读取配置仓库指定 ref 的有界 UTF-8 文本；确认当前逻辑时 ref 使用 running commit。

    Args:
        service: ServiceRegistry 中已配置的服务名称
        path: 仓库内相对文件路径，不允许目录穿越、绝对路径或 URL
        ref: 指定 commit SHA 或 Git ref；调查当前版本时用 get_running_version 的完整 SHA
    """
    repository,error=repository_for(service)
    if error:
        return {"status":"no_data","service":service,"reason":error}
    repo=repository["repo"]
    result={"status":"no_data","service":service,"repository":repo}
    if not valid_path(path):
        return {**result,"reason":"invalid_path"}
    if not valid_ref(ref):
        return {**result,"reason":"invalid_ref"}
    try:
        body=github_client.get_file_content(repo,path,ref)
        if not isinstance(body,dict) or body.get("type")!="file":
            return {**result,"reason":"unsupported_file_type"}
        size=body.get("size")
        if type(size) is not int or size<0 or body.get("path")!=path:
            return {**result,"reason":"invalid_github_response"}
        if body.get("encoding")=="base64" and isinstance(body.get("content"),str):
            raw=base64.b64decode("".join(body["content"].split()),validate=True)
            if len(raw)!=size:
                return {**result,"reason":"invalid_github_response"}
            content=raw.decode("utf-8")
            byte_truncated=False
        elif body.get("encoding")=="none" and size>1_000_000:
            raw=github_client.get_file_prefix(repo,path,ref,MAX_SOURCE_BYTES)
            byte_truncated=len(raw)<size
            decoder=codecs.getincrementaldecoder("utf-8")("strict")
            content=decoder.decode(raw,final=not byte_truncated)
        else:
            return {**result,"reason":"unsupported_encoding"}
    except (UnicodeError,binascii.Error):
        return {**result,"reason":"binary_or_unsupported"}
    except (requests.RequestException,ValueError) as exc:
        return read_error(service,repo,exc)
    if any(ord(c)<32 and c not in "\t\n\r" for c in content):
        return {**result,"reason":"binary_or_unsupported"}
    return {**result,"status":"ok","path":path,"ref":ref,"blob_sha":body.get("sha"),
            "content":content[:MAX_SOURCE_CHARS],"size_bytes":size,
            "truncated":byte_truncated or len(content)>MAX_SOURCE_CHARS,
            "limit_chars":MAX_SOURCE_CHARS}
