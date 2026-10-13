"""WorkRally MCP 的 OAuth 2.1：发现、动态注册、PKCE 授权、刷新。

没有用 mcp SDK 自带的 OAuthClientProvider：它在新进程里不知道 token 何时过期，
收到 401 会直接走完整的浏览器授权而不是刷新。CLI 每条命令都是新进程，所以这里自己管。

refresh_token 每次刷新都会轮换，因此刷新必须在文件锁内进行，并立即原子写回。
"""

import base64
import hashlib
import http.server
import os
import secrets
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from filelock import FileLock
from pydantic import BaseModel

from wrally.config import CLIENT_NAME, SCOPE, config_dir
from wrally.errors import AuthError, NetworkError

EXPIRY_SKEW = 300  # 提前 5 分钟刷新，避免长时间的 wait/fetch 中途过期


class Credentials(BaseModel):
    client_id: str
    redirect_uri: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    resource: str
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0
    scope: str = SCOPE


class CredentialStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or config_dir()
        self.path = self.dir / "credentials.json"

    def lock(self) -> FileLock:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return FileLock(str(self.dir / "credentials.lock"), timeout=60)

    def load(self) -> Credentials | None:
        try:
            return Credentials.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def save(self, creds: Credentials) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(creds.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


def _get_json(http: httpx2.Client, urls: list[str]) -> dict:
    """依次尝试，取第一个返回 JSON 对象的地址（不存在的路径会返回 200 的网页）。"""
    for url in urls:
        try:
            r = http.get(url, headers={"Accept": "application/json"})
        except httpx2.TransportError as e:
            raise NetworkError(hint=f"{type(e).__name__} {urlsplit(url).netloc}") from e
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    raise AuthError("oauth_discovery_failed")


def discover(http: httpx2.Client, mcp_url: str) -> dict:
    """返回授权服务器元数据（issuer、authorization_endpoint、token_endpoint、registration_endpoint）。"""
    u = urlsplit(mcp_url)
    origin = f"{u.scheme}://{u.netloc}"
    prm = _get_json(
        http,
        [
            f"{origin}/.well-known/oauth-protected-resource{u.path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ],
    )
    servers = prm.get("authorization_servers") or []
    if not servers:
        raise AuthError("oauth_discovery_failed")
    issuer = servers[0].rstrip("/")
    i = urlsplit(issuer)
    as_origin = f"{i.scheme}://{i.netloc}"
    meta = _get_json(
        http,
        [
            f"{as_origin}/.well-known/oauth-authorization-server{i.path}",
            f"{as_origin}/.well-known/oauth-authorization-server",
            f"{issuer}/.well-known/oauth-authorization-server",
        ],
    )
    for key in ("authorization_endpoint", "token_endpoint"):
        if not meta.get(key):
            raise AuthError("oauth_discovery_failed")
    meta.setdefault("issuer", issuer)
    return meta


class _CallbackServer(http.server.HTTPServer):
    params: dict[str, str]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        self.server.params = {k: v[0] for k, v in query.items()}  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h3>wrally 已收到授权回调，可以关闭此页。</h3>".encode())

    def log_message(self, *args: object) -> None:
        pass


def _bind(port: int) -> _CallbackServer | None:
    try:
        server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    except OSError:
        return None
    server.params = {}
    server.timeout = 1
    return server


def _register(http: httpx2.Client, meta: dict, redirect_uri: str) -> str:
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise AuthError("oauth_registration_unsupported")
    r = _post(
        http,
        endpoint,
        json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
    )
    if r.status_code not in (200, 201):
        raise AuthError("oauth_registration_failed", f"HTTP {r.status_code}")
    return r.json()["client_id"]


def _post(http: httpx2.Client, url: str, **kwargs: object) -> httpx2.Response:
    try:
        return http.post(url, **kwargs)  # type: ignore[arg-type]
    except httpx2.TransportError as e:
        raise NetworkError(hint=f"{type(e).__name__} {urlsplit(url).netloc}") from e


def _apply_token(creds: Credentials, token: dict) -> Credentials:
    return creds.model_copy(
        update={
            "access_token": token["access_token"],
            # 服务端不返回新的 refresh_token 时沿用旧的
            "refresh_token": token.get("refresh_token") or creds.refresh_token,
            "expires_at": time.time() + float(token.get("expires_in") or 0),
            "scope": token.get("scope") or creds.scope,
        }
    )


def login(
    store: CredentialStore,
    http: httpx2.Client,
    mcp_url: str,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = 300,
    notify: Callable[[str], None] = print,
) -> Credentials:
    """浏览器授权并保存凭证。已注册过且回调端口可用时复用原来的 client_id。"""
    meta = discover(http, mcp_url)
    old = store.load()
    server = None
    client_id = ""
    if old and old.issuer == meta["issuer"]:
        server = _bind(urlsplit(old.redirect_uri).port or 0)
        if server:
            client_id, redirect_uri = old.client_id, old.redirect_uri
    if server is None:
        server = _bind(0)
        if server is None:
            raise AuthError("oauth_callback_port_unavailable")
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
        client_id = _register(http, meta, redirect_uri)

    try:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = (
            meta["authorization_endpoint"]
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "scope": SCOPE,
                    "resource": mcp_url,
                }
            )
        )
        notify("waiting for browser authorization ...")
        if not open_browser(url):
            notify(f"open this URL to authorize: {url}")
        deadline = time.monotonic() + timeout
        while not server.params and time.monotonic() < deadline:
            server.handle_request()
        params = server.params
    finally:
        server.server_close()

    if not params:
        raise AuthError("oauth_timeout")
    if params.get("error"):
        raise AuthError("oauth_denied", params["error"])
    if params.get("state") != state or not params.get("code"):
        raise AuthError("oauth_state_mismatch")
    if params.get("iss") and params["iss"].rstrip("/") != meta["issuer"].rstrip("/"):
        raise AuthError("oauth_issuer_mismatch")

    r = _post(
        http,
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": mcp_url,
        },
    )
    if r.status_code != 200:
        raise AuthError("oauth_token_exchange_failed", f"HTTP {r.status_code}")
    creds = _apply_token(
        Credentials(
            client_id=client_id,
            redirect_uri=redirect_uri,
            issuer=meta["issuer"],
            authorization_endpoint=meta["authorization_endpoint"],
            token_endpoint=meta["token_endpoint"],
            resource=mcp_url,
        ),
        r.json(),
    )
    with store.lock():
        store.save(creds)
    return creds


def _refresh(http: httpx2.Client, creds: Credentials) -> Credentials:
    # 不重试：refresh_token 会轮换，请求可能已被服务端处理，重发旧值只会失败
    r = _post(
        http,
        creds.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
            "client_id": creds.client_id,
            "resource": creds.resource,
        },
    )
    if r.status_code in (400, 401):
        raise AuthError("login_expired", "(run: wrally login)")
    if r.status_code != 200:
        raise NetworkError("token_refresh_failed", f"HTTP {r.status_code}")
    return _apply_token(creds, r.json())


def _require(creds: Credentials | None) -> Credentials:
    if creds is None or not creds.access_token:
        raise AuthError("not_logged_in", "(run: wrally login)")
    return creds


def ensure_fresh(store: CredentialStore, http: httpx2.Client) -> Credentials:
    """返回可用凭证；临近过期时在锁内刷新并写回。"""
    with store.lock():
        creds = _require(store.load())
        if creds.expires_at - EXPIRY_SKEW > time.time():
            return creds
        if not creds.refresh_token:
            raise AuthError("login_expired", "(run: wrally login)")
        creds = _refresh(http, creds)
        store.save(creds)
        return creds


def refresh_after_401(store: CredentialStore, http: httpx2.Client, stale_access_token: str) -> Credentials:
    """服务端拒绝了 stale_access_token。若别的进程已经刷新过，直接用它写回的新值。"""
    with store.lock():
        creds = _require(store.load())
        if creds.access_token != stale_access_token:
            return creds
        if not creds.refresh_token:
            raise AuthError("login_expired", "(run: wrally login)")
        creds = _refresh(http, creds)
        store.save(creds)
        return creds


def dump_public(creds: Credentials) -> dict:
    """可以打印的凭证摘要，不含 token。"""
    return {
        "client_id": creds.client_id,
        "expires_in": max(0, int(creds.expires_at - time.time())),
        "scope": creds.scope,
        "has_refresh_token": bool(creds.refresh_token),
    }


__all__ = [
    "Credentials",
    "CredentialStore",
    "discover",
    "login",
    "ensure_fresh",
    "refresh_after_401",
    "dump_public",
]
