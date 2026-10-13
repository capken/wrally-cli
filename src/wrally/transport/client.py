"""MCP 客户端：连接 WorkRally、调用工具、解析结果、落盘原始返回。

重试：网络超时和 5xx 按 1s、4s、16s 指数退避 3 次；业务错误不重试。
提交生成任务的调用要传 retry=False：超时后无法判断服务端是否已受理，重发可能重复消耗额度。
"""

import json
import os
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.exceptions import MCPError

from wrally import __version__
from wrally.config import CLIENT_NAME, mcp_url
from wrally.errors import NetworkError, ToolError, WrallyError
from wrally.transport.auth import EXPIRY_SKEW, Credentials, CredentialStore, ensure_fresh, refresh_after_401

RETRY_DELAYS = (1, 4, 16)
CALL_TIMEOUT = 120
# SDK 把 HTTP 4xx/5xx 统一报成 INTERNAL_ERROR，无法区分，一并按可重试处理
_RETRYABLE_MCP_CODES = {types.INTERNAL_ERROR, types.REQUEST_TIMEOUT, types.CONNECTION_CLOSED}
_RETRYABLE = (httpx2.TransportError, TimeoutError, anyio.BrokenResourceError, anyio.ClosedResourceError)


class BearerAuth(httpx2.Auth):
    """给每个请求带上 access_token；临近过期或收到 401 时刷新一次。"""

    def __init__(self, store: CredentialStore, http: httpx2.Client, creds: Credentials | None = None) -> None:
        self._store = store
        self._http = http
        self._creds = creds

    async def async_auth_flow(self, request: httpx2.Request):
        if self._creds is None or self._creds.expires_at - EXPIRY_SKEW <= time.time():
            self._creds = await anyio.to_thread.run_sync(ensure_fresh, self._store, self._http)
        request.headers["Authorization"] = f"Bearer {self._creds.access_token}"
        response = yield request
        if response.status_code == 401:
            self._creds = await anyio.to_thread.run_sync(
                refresh_after_401, self._store, self._http, self._creds.access_token
            )
            request.headers["Authorization"] = f"Bearer {self._creds.access_token}"
            yield request


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, MCPError):
        return getattr(exc, "code", None) in _RETRYABLE_MCP_CODES or "Session terminated" in str(exc)
    return False


def _find_wrally_error(exc: BaseException) -> WrallyError | None:
    """SDK 的任务组会把鉴权错误包进 ExceptionGroup，这里把它找出来。"""
    if isinstance(exc, WrallyError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _find_wrally_error(inner)
            if found:
                return found
    cause = exc.__cause__ or exc.__context__
    return _find_wrally_error(cause) if cause else None


def write_raw(path: Path, payload: Any) -> None:
    """原始返回含提示词和签名地址，只落盘、不打印，权限 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def parse_result(tool: str, result: Any, raw_path: Path | None = None) -> Any:
    """把工具结果解析成 JSON。结果通常只有 text 内容块，里面是 JSON 字符串。"""
    text = "".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", None) == "text")
    data = result.structured_content
    if data is None:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if raw_path is not None:
        write_raw(
            raw_path,
            {"tool": tool, "is_error": bool(result.is_error), "data": text if data is None else data},
        )
    if result.is_error:
        raise ToolError("tool_error", tool=tool, raw_path=raw_path)
    if data is None:
        raise ToolError("bad_response", tool=tool, raw_path=raw_path)
    head = data.get("head") if isinstance(data, dict) else None
    if isinstance(head, dict) and head.get("code") not in (0, None):
        raise ToolError(str(head["code"]), tool=tool, raw_path=raw_path)
    return data


class McpClient:
    """异步上下文管理器；一条命令内复用同一个会话。"""

    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        url: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store or CredentialStore()
        self._url = url or mcp_url()
        self._log = log
        self._auth_http: httpx2.Client | None = None
        self._creds: Credentials | None = None
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpClient":
        self._auth_http = httpx2.Client(timeout=30)
        try:
            # 先在 SDK 之外检查登录状态，未登录时能直接给出干净的错误
            self._creds = ensure_fresh(self._store, self._auth_http)
            await self._connect()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._disconnect()
        if self._auth_http is not None:
            self._auth_http.close()
            self._auth_http = None

    async def _connect(self) -> None:
        assert self._auth_http is not None
        stack = AsyncExitStack()
        try:
            http = await stack.enter_async_context(
                create_mcp_http_client(
                    auth=BearerAuth(self._store, self._auth_http, self._creds),
                    timeout=httpx2.Timeout(30, read=CALL_TIMEOUT + 30),
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client(self._url, http_client=http, max_sse_event_size=16 * 1024 * 1024)
            )
            session = await stack.enter_async_context(
                ClientSession(
                    streams[0],
                    streams[1],
                    client_info=types.Implementation(name=CLIENT_NAME, version=__version__),
                )
            )
            await session.initialize()
        except BaseException as e:
            await stack.aclose()
            found = _find_wrally_error(e)
            if found:
                raise found from e
            if isinstance(e, Exception):
                raise NetworkError("connect_failed", type(e).__name__) from e
            raise
        self._stack, self._session = stack, session

    async def _disconnect(self) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    async def call(
        self, tool: str, arguments: dict[str, Any], *, raw_path: Path | None = None, retry: bool = True
    ) -> Any:
        delays = RETRY_DELAYS if retry else ()
        for attempt in range(len(delays) + 1):
            if self._session is None:
                await self._connect()
            assert self._session is not None
            started = time.monotonic()
            try:
                result = await self._session.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT)
            except Exception as e:
                found = _find_wrally_error(e)
                if found:
                    raise found from e
                if not _is_retryable(e):
                    raise ToolError("mcp_error", tool=tool) from e
                await self._disconnect()
                if attempt == len(delays):
                    raise NetworkError(hint=f"{tool} {type(e).__name__}") from e
                await anyio.sleep(delays[attempt])
                continue
            if self._log:
                self._log(f"call {tool} {time.monotonic() - started:.1f}s")
            return parse_result(tool, result, raw_path)
        raise AssertionError("unreachable")
