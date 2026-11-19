"""MCP 客户端：连接 WorkRally、调用工具、解析结果、落盘原始返回。

重试：网络超时和 5xx 按 1s、4s、16s 指数退避 3 次；业务错误不重试。
提交生成任务的调用要传 retry=False：超时后无法判断服务端是否已受理，重发可能重复消耗额度。
"""

import json
import math
import os
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx2
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
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


def _leading_json(text: str) -> Any:
    """文字开头的那个 JSON，后面可以跟别的文字。

    project_create 在 JSON 后面加了一行「📎 项目管理页面: <地址>」。
    """
    try:
        data, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return None
    return data


def parse_result(tool: str, result: Any, raw_path: Path | None = None) -> Any:
    """把工具结果解析成 JSON。结果通常只有 text 内容块，里面是 JSON 字符串。"""
    text = "".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", None) == "text")
    data = result.structured_content
    if data is None:
        data = _leading_json(text)
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


@dataclass
class _SessionReq:
    op: Literal["acquire", "drop"]
    failed_gen: int | None
    done: anyio.Event = field(default_factory=anyio.Event)
    result: tuple[Any, int] | None = None
    error: BaseException | None = None


class McpClient:
    """异步上下文管理器；一条命令内复用同一个会话。

    底层会话（AsyncExitStack + streamable_http_client + ClientSession）由内部唯一一个 owner 任务
    打开和关闭：并发调用（for_each）里某一路遇到可重试错误时，不会跨 task 关闭 cancel scope，
    多路同时失败也只会重连一次。
    """

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
        self._tg: Any = None
        self._send: MemoryObjectSendStream[_SessionReq] | None = None

    async def __aenter__(self) -> "McpClient":
        self._auth_http = httpx2.Client(timeout=30)
        try:
            # 先在 SDK 之外检查登录状态，未登录时能直接给出干净的错误
            self._creds = ensure_fresh(self._store, self._auth_http)
            send, recv = anyio.create_memory_object_stream[_SessionReq](math.inf)
            self._send = send
            tg = anyio.create_task_group()
            await tg.__aenter__()
            self._tg = tg
            tg.start_soon(self._owner, recv)
            await self._acquire(None)
        except BaseException as e:
            await self.__aexit__(type(e), e, e.__traceback__)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        send, self._send = self._send, None
        tg, self._tg = self._tg, None
        try:
            with anyio.CancelScope(shield=True):
                if send is not None:
                    await send.aclose()
            if tg is not None:
                if exc and exc[0] is not None:
                    tg.cancel_scope.cancel()
                await tg.__aexit__(None, None, None)
        finally:
            if self._auth_http is not None:
                self._auth_http.close()
                self._auth_http = None

    async def _open_session(self) -> tuple[AsyncExitStack, Any]:
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
        return stack, session

    async def _owner(self, recv: MemoryObjectReceiveStream[_SessionReq]) -> None:
        stack: AsyncExitStack | None = None
        session: Any = None
        gen = 0

        async def close_session() -> None:
            nonlocal stack, session
            old, stack, session = stack, None, None
            if old is not None:
                try:
                    await old.aclose()
                except Exception:
                    pass

        try:
            async with recv:
                async for req in recv:
                    try:
                        # 只有报错的是当前这一代会话才断开：别路已经重连好新会话时直接复用
                        if req.failed_gen is not None and req.failed_gen == gen:
                            await close_session()
                        if req.op == "acquire":
                            if session is None:
                                stack, session = await self._open_session()
                                gen += 1
                            req.result = (session, gen)
                    except BaseException as e:
                        req.error = e
                        if not isinstance(e, Exception):
                            req.done.set()
                            raise
                    finally:
                        req.done.set()
        finally:
            await close_session()

    async def _request(
        self, op: Literal["acquire", "drop"], failed_gen: int | None
    ) -> tuple[Any, int] | None:
        if self._send is None:
            raise RuntimeError("McpClient is not connected")
        req = _SessionReq(op=op, failed_gen=failed_gen)
        await self._send.send(req)
        await req.done.wait()
        if req.error is not None:
            raise req.error
        return req.result

    async def _acquire(self, failed_gen: int | None) -> tuple[Any, int]:
        got = await self._request("acquire", failed_gen)
        assert got is not None
        return got

    async def _drop(self, failed_gen: int) -> None:
        await self._request("drop", failed_gen)

    async def call(
        self, tool: str, arguments: dict[str, Any], *, raw_path: Path | None = None, retry: bool = True
    ) -> Any:
        delays = RETRY_DELAYS if retry else ()
        failed_gen: int | None = None
        for attempt in range(len(delays) + 1):
            started = time.monotonic()
            gen: int | None = None
            try:
                session, gen = await self._acquire(failed_gen)
                failed_gen = None
                result = await session.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT)
            except Exception as e:
                found = _find_wrally_error(e)
                if found:
                    if not isinstance(found, NetworkError) or attempt == len(delays):
                        if gen is not None and isinstance(found, NetworkError):
                            await self._drop(gen)
                        raise found from e
                    failed_gen = gen
                    await anyio.sleep(delays[attempt])
                    continue
                if not _is_retryable(e):
                    raise ToolError("mcp_error", tool=tool) from e
                if attempt == len(delays):
                    if gen is not None:
                        await self._drop(gen)
                    raise NetworkError(hint=f"{tool} {type(e).__name__}") from e
                failed_gen = gen
                await anyio.sleep(delays[attempt])
                continue
            if self._log:
                self._log(f"call {tool} {time.monotonic() - started:.1f}s")
            return parse_result(tool, result, raw_path)
        raise AssertionError("unreachable")
