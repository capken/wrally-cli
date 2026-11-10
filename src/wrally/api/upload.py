"""把本地文件上传成 WorkRally 媒资，返回 asset_id。

upload_file 读的是 MCP 服务那台机器上的文件，本机文件用不了它。所以分三步：
get_upload_token 取 COS 临时凭证 → 自己签名直传到凭证给的 key → asset_create 入库。
直传不能带 Content-Type 等头（凭证的策略不允许），只签 host。凭证和上传地址只在本模块里流转。

同一个文件（按 SHA-1）在同一个项目里只传一次：传过的记在凭证目录的 uploads.json 里，
用之前先 asset_detail 确认媒资还在，不在了（被删了）就重新上传。
"""

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import anyio
import httpx2

from wrally import media
from wrally.api.assets import asset_details
from wrally.config import config_dir
from wrally.errors import NetworkError, PreflightError, ToolError, WrallyError
from wrally.localfile import check_file, sha1
from wrally.transport.client import RETRY_DELAYS, McpClient

CACHE = "uploads.json"  # 在凭证目录下
TOKEN_BATCH = 20  # get_upload_token 一次最多 20 个
DEFAULT_PROJECT = "默认项目"  # 没指定项目时素材归到这里（asset_create 的说明要求素材都关联一个项目）


# ---------- 上传记录 ----------


def _cache_path() -> Path:
    return config_dir() / CACHE


def load_cache() -> dict[str, dict]:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8")).get("files") or {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_cache(files: dict[str, dict]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps({"version": 1, "files": files}, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _cache_key(digest: str, project_id: str) -> str:
    return f"{digest}:{project_id}"


def cached(path: Path, project_id: str | None) -> str | None:
    """本地记录里这个文件在这个项目的媒资 ID；不调远端，也不确认媒资还在。dry run 用。"""
    if not project_id:
        return None
    hit = load_cache().get(_cache_key(sha1(path), project_id))
    return hit.get("asset_id") if hit else None


# ---------- 远端 ----------


async def default_project(client: McpClient) -> str:
    """名为「默认项目」的项目 ID；没有就报错，要求显式给 --project。"""
    from wrally.api.projects import project_list

    found = [p for p in await project_list(client, search=DEFAULT_PROJECT) if p.name == DEFAULT_PROJECT]
    if len(found) != 1:
        hint = f"(no single project named {DEFAULT_PROJECT}; pass --project)"
        raise PreflightError("missing_project", hint)
    return found[0].project_id


def _sign(secret_id: str, secret_key: str, method: str, path: str, host: str) -> str:
    """COS 请求签名（只签 host 头，不签查询参数）。"""
    now = int(time.time())
    key_time = f"{now - 60};{now + 900}"
    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    http_string = f"{method.lower()}\n{path}\n\nhost={quote(host, safe='')}\n"
    to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    signature = hmac.new(sign_key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
    return (
        f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}&q-key-time={key_time}"
        f"&q-header-list=host&q-url-param-list=&q-signature={signature}"
    )


async def _put(http: httpx2.AsyncClient, token: dict, body: bytes, name: str) -> None:
    """按凭证直传一个文件。网络错误和 5xx 退避重试（同一把凭证、同一个 key，重传是覆盖）。"""
    host = f"{token['bucket']}.cos.{token['region']}.myqcloud.com"
    key = "/" + str(token["key"]).lstrip("/")
    for attempt in range(len(RETRY_DELAYS) + 1):
        headers = {
            "Authorization": _sign(token["tmp_secret_id"], token["tmp_secret_key"], "PUT", key, host),
            "x-cos-security-token": token["tmp_token"],
        }
        try:
            r = await http.put(f"https://{host}{quote(key, safe='/-_.~')}", content=body, headers=headers)
        except httpx2.TransportError as e:
            if attempt == len(RETRY_DELAYS):
                raise NetworkError(hint=f"(upload {name}: {type(e).__name__})") from e
        else:
            if r.status_code == 200:
                return
            if r.status_code < 500 or attempt == len(RETRY_DELAYS):
                # 返回体里可能带签名信息，不打印
                raise ToolError(f"upload_http_{r.status_code}", tool="cos_put")
        await anyio.sleep(RETRY_DELAYS[attempt])


def _meta(path: Path, kind: str) -> dict:
    """入库时带上的宽高和时长（毫秒）：网页上靠它们正常显示。量不出来就不带。"""
    try:
        info = media.probe(path)
    except WrallyError:
        return {}  # 没装 ffprobe
    meta: dict = {}
    if kind in ("image", "video") and info.get("width") and info.get("height"):
        meta |= {"width": int(info["width"]), "height": int(info["height"])}
    if kind in ("video", "audio") and info.get("duration"):
        meta["duration"] = int(round(info["duration"] * 1000))
    return meta


@dataclass
class Uploaded:
    path: Path
    asset_id: str
    state: str  # uploaded / cached


async def upload(
    client: McpClient,
    paths: list[Path],
    project_id: str,
    *,
    http: httpx2.AsyncClient | None = None,
) -> list[Uploaded]:
    """上传一批本地文件，返回和 paths 一一对应的结果。传过且媒资还在的不再上传。

    asset_create 不自动重试：超时后不知道是否已入库，重发会多出一条同样的素材。
    每入库一个就写一次上传记录，中途出错时前面的不丢。
    """
    kinds = [check_file(p) for p in paths]
    digests = [sha1(p) for p in paths]
    files = load_cache()
    keys = [_cache_key(d, project_id) for d in digests]
    known = {files[k]["asset_id"] for k in keys if k in files}
    alive = set(await asset_details(client, sorted(known))) if known else set()
    found: dict[str, tuple[str, str]] = {
        k: (files[k]["asset_id"], "cached") for k in keys if k in files and files[k]["asset_id"] in alive
    }
    first = {k: i for i, k in reversed(list(enumerate(keys)))}  # 同一个文件给了几次也只传一次
    todo = sorted(i for k, i in first.items() if k not in found)
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=httpx2.Timeout(30, write=300))
    try:
        for start in range(0, len(todo), TOKEN_BATCH):
            batch = todo[start : start + TOKEN_BATCH]
            exts = [paths[i].suffix.lower().lstrip(".") for i in batch]
            data = await client.call("get_upload_token", {"file_exts": exts})
            tokens = data.get("files") or ([data] if data.get("key") else [])
            if len(tokens) != len(batch):
                raise ToolError("bad_upload_token", tool="get_upload_token")
            for i, token in zip(batch, tokens, strict=True):
                path = paths[i]
                body = path.read_bytes()
                await _put(http, token, body, path.name)
                args = {
                    "asset_url": token["file_url"],
                    "asset_type": kinds[i],
                    "name": path.name,
                    "project_id": project_id,
                    "format": path.suffix.lower().lstrip("."),
                    "file_size": len(body),
                    "sha1": digests[i],
                    **_meta(path, kinds[i]),
                }
                made = await client.call("asset_create", args, retry=False)
                asset_id = next((a.get("id") for a in made.get("assets") or [] if a.get("id")), None)
                if not asset_id:
                    raise ToolError("asset_id_not_found", tool="asset_create")
                files[keys[i]] = {
                    "asset_id": asset_id,
                    "name": path.name,
                    "size": len(body),
                    "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save_cache(files)
                found[keys[i]] = (asset_id, "uploaded")
    finally:
        if owned:
            await http.aclose()
    out, seen = [], set()
    for path, key in zip(paths, keys, strict=True):
        asset_id, state = found[key]
        # 同一次里重复给的同一个文件，第二次起算 cached
        out.append(Uploaded(path, asset_id, "cached" if key in seen else state))
        seen.add(key)
    return out
