import stat
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest

from wrally.errors import AuthError, NetworkError
from wrally.transport import auth
from wrally.transport.client import BearerAuth

MCP = "https://wr.example.com/zenstudio/api/mcp"
ISSUER = "https://wr.example.com/zenstudio/oauth"
META = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "registration_endpoint": f"{ISSUER}/register",
}


def creds(**over) -> auth.Credentials:
    base = dict(
        client_id="cid",
        redirect_uri="http://127.0.0.1:1/callback",
        issuer=ISSUER,
        authorization_endpoint=META["authorization_endpoint"],
        token_endpoint=META["token_endpoint"],
        resource=MCP,
        access_token="at-old",
        refresh_token="rt-old",
        expires_at=time.time() + 3600,
    )
    return auth.Credentials(**(base | over))


class Server:
    """模拟授权服务器：记录请求，每次刷新都轮换 refresh_token。"""

    def __init__(self, token_status=200):
        self.token_status = token_status
        self.token_requests: list[dict] = []
        self.registrations = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource/zenstudio/api/mcp":
            return httpx2.Response(200, json={"authorization_servers": [ISSUER]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json=META)
        if path.startswith("/.well-known/"):
            return httpx2.Response(200, text="<html>单页应用兜底页</html>")
        if path.endswith("/register"):
            self.registrations += 1
            return httpx2.Response(201, json={"client_id": f"cid{self.registrations}"})
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if self.token_status != 200:
                return httpx2.Response(self.token_status, json={"error": "invalid_grant"})
            n = len(self.token_requests)
            return httpx2.Response(
                200,
                json={
                    "access_token": f"at{n}",
                    "refresh_token": f"rt{n}",
                    "expires_in": 14400,
                    "scope": "mcp",
                },
            )
        return httpx2.Response(404)

    def http(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))


@pytest.fixture
def store(tmp_path):
    return auth.CredentialStore(tmp_path / "cfg")


def test_store_writes_0600_and_roundtrips(store):
    store.save(creds())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load().refresh_token == "rt-old"
    assert not store.path.with_suffix(".tmp").exists()


def test_discover_skips_html_fallback_pages():
    assert auth.discover(Server().http(), MCP)["token_endpoint"] == f"{ISSUER}/token"


def test_ensure_fresh_requires_login(store):
    with pytest.raises(AuthError) as e:
        auth.ensure_fresh(store, Server().http())
    assert e.value.code == "not_logged_in" and e.value.exit_code == 3


def test_ensure_fresh_keeps_valid_token(store):
    server = Server()
    store.save(creds())
    assert auth.ensure_fresh(store, server.http()).access_token == "at-old"
    assert server.token_requests == []


def test_ensure_fresh_refreshes_near_expiry_and_persists_rotation(store):
    """access_token 4 小时过期；refresh_token 每次刷新轮换，新值必须立即写回。"""
    server = Server()
    store.save(creds(expires_at=time.time() + 60))
    fresh = auth.ensure_fresh(store, server.http())
    assert (fresh.access_token, fresh.refresh_token) == ("at1", "rt1")
    assert server.token_requests[0]["refresh_token"] == "rt-old"
    assert store.load().refresh_token == "rt1"
    assert store.load().expires_at > time.time() + 14000


def test_refresh_rejected_means_login_again(store):
    store.save(creds(expires_at=0))
    with pytest.raises(AuthError) as e:
        auth.ensure_fresh(store, Server(token_status=400).http())
    assert e.value.code == "login_expired"
    assert store.load().refresh_token == "rt-old"  # 失败时不破坏已有凭证


def test_refresh_5xx_is_network_error_and_not_retried(store):
    server = Server(token_status=502)
    store.save(creds(expires_at=0))
    with pytest.raises(NetworkError):
        auth.ensure_fresh(store, server.http())
    assert len(server.token_requests) == 1


def test_refresh_after_401_reuses_token_refreshed_by_another_process(store):
    """并发刷新：别的进程已经轮换过，就不能再拿旧的 refresh_token 去刷。"""
    server = Server()
    store.save(creds(access_token="at-new", refresh_token="rt-new"))
    got = auth.refresh_after_401(store, server.http(), "at-old")
    assert got.access_token == "at-new"
    assert server.token_requests == []


def test_refresh_after_401_refreshes_when_token_is_still_the_stale_one(store):
    server = Server()
    store.save(creds())
    assert auth.refresh_after_401(store, server.http(), "at-old").access_token == "at1"
    assert store.load().refresh_token == "rt1"


def _browser(redirects: list[str], **override):
    """假浏览器：解析授权地址后回调本地端口。"""

    def open_browser(url: str) -> bool:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        redirects.append(q["redirect_uri"])
        params = {"code": "code1", "state": q["state"], "iss": ISSUER} | override
        target = q["redirect_uri"] + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        threading.Thread(target=lambda: urllib.request.urlopen(target).read(), daemon=True).start()
        return True

    return open_browser


def test_login_registers_authorizes_with_pkce_and_saves(store):
    server, redirects = Server(), []
    got = auth.login(store, server.http(), MCP, open_browser=_browser(redirects), notify=lambda _: None)
    form = server.token_requests[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "code1"
    assert len(form["code_verifier"]) >= 43 and form["resource"] == MCP
    assert got.client_id == "cid1" and got.redirect_uri == redirects[0]
    assert store.load().access_token == "at1"


def test_login_reuses_registered_client(store):
    server, redirects = Server(), []
    first = auth.login(store, server.http(), MCP, open_browser=_browser(redirects), notify=lambda _: None)
    second = auth.login(store, server.http(), MCP, open_browser=_browser(redirects), notify=lambda _: None)
    assert server.registrations == 1
    assert second.client_id == first.client_id and redirects[0] == redirects[1]


def test_login_rejects_state_mismatch(store):
    with pytest.raises(AuthError) as e:
        auth.login(
            store, Server().http(), MCP, open_browser=_browser([], state="forged"), notify=lambda _: None
        )
    assert e.value.code == "oauth_state_mismatch"
    assert store.load() is None


def test_bearer_auth_refreshes_once_on_401(store):
    server = Server()
    store.save(creds())
    seen = []

    def api(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["Authorization"])
        return httpx2.Response(401 if len(seen) == 1 else 200)

    async def run() -> int:
        bearer = BearerAuth(store, server.http())
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(api), auth=bearer) as client:
            return (await client.post(MCP)).status_code

    assert anyio.run(run) == 200
    assert seen == ["Bearer at-old", "Bearer at1"]
