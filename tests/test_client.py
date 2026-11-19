import json
import stat
from contextlib import AsyncExitStack
from types import SimpleNamespace

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import MCPError

from wrally.commands.common import for_each
from wrally.errors import AuthError, NetworkError, ToolError
from wrally.transport import client as client_mod
from wrally.transport.client import McpClient, parse_result


def result(text, *, is_error=False, structured=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], structured_content=structured, is_error=is_error
    )


def test_parses_json_from_text_block():
    assert parse_result("t", result('{"head": {"code": 0}, "story_list": []}'))["story_list"] == []


def test_json_followed_by_a_note_line():
    """project_create 在 JSON 后面加了一行带链接的说明（2026-11-09 真实遇到）。"""
    text = '{"head": {"code": 0}, "project_id": "p1"}\n📎 项目管理页面: https://example.com/p/p1'
    assert parse_result("project_create", result(text))["project_id"] == "p1"


def test_prefers_structured_content():
    assert parse_result("t", result("ignored", structured={"status": "success"})) == {"status": "success"}


def test_raw_is_written_before_raising_and_is_private(tmp_path):
    raw = tmp_path / "raw" / "10-video.json"
    with pytest.raises(ToolError) as e:
        parse_result("gen", result('{"head": {"code": 40012, "msg": "审核拒绝"}}'), raw)
    assert e.value.code == "40012" and e.value.raw_path == raw
    assert "审核拒绝" not in str(e.value)  # 终端只打错误码和路径
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    assert "审核拒绝" in raw.read_text(encoding="utf-8")  # 中文不转义
    assert json.loads(raw.read_text(encoding="utf-8"))["tool"] == "gen"


def test_tool_error_flag_and_non_json():
    with pytest.raises(ToolError) as e:
        parse_result("t", result("something went wrong", is_error=True))
    assert e.value.code == "tool_error"
    with pytest.raises(ToolError) as e:
        parse_result("t", result("not json"))
    assert e.value.code == "bad_response"


async def _dummy_session_stack(opened: list[int], closed: list[int], gen: int, call_tool):
    """带真实 anyio task group 的会话栈：跨 task 关闭会触发 RuntimeError，用来验证 owner 单任务管理。"""
    stack = AsyncExitStack()
    await stack.enter_async_context(anyio.create_task_group())
    opened.append(gen)
    stack.callback(closed.append, gen)
    return stack, SimpleNamespace(call_tool=call_tool)


@pytest.mark.anyio
async def test_concurrent_calls_share_a_single_reconnect_and_keep_cancel_scopes_in_one_task(monkeypatch):
    monkeypatch.setattr(client_mod, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(client_mod, "ensure_fresh", lambda store, http: SimpleNamespace(access_token="tok"))
    opened, closed = [], []

    async def open_session(self):
        gen = len(opened) + 1

        async def call_tool(tool, arguments, *, read_timeout_seconds):
            if gen == 1:
                raise anyio.BrokenResourceError("dropped")
            return result(json.dumps({"head": {"code": 0}, "item": arguments["n"], "gen": gen}))

        return await _dummy_session_stack(opened, closed, gen, call_tool)

    monkeypatch.setattr(McpClient, "_open_session", open_session)
    async with McpClient() as mcp:
        out = await for_each([1, 2, 3, 4], lambda n: mcp.call("t", {"n": n}))

    assert [x["item"] for x in out] == [1, 2, 3, 4]
    assert all(x["gen"] == 2 for x in out)
    # 4 路并发同时失败，只重连了一次，且两代会话都干净关闭（没有跨 task 关闭 cancel scope）
    assert opened == [1, 2] and closed == [1, 2]


@pytest.mark.anyio
async def test_retry_false_never_retries_and_drops_broken_session(monkeypatch):
    monkeypatch.setattr(client_mod, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(client_mod, "ensure_fresh", lambda store, http: SimpleNamespace(access_token="tok"))
    opened, closed, calls = [], [], []

    async def open_session(self):
        gen = len(opened) + 1

        async def call_tool(tool, arguments, *, read_timeout_seconds):
            calls.append((gen, tool))
            if gen == 1:
                raise TimeoutError("timed out")
            return result('{"head": {"code": 0}, "ok": true}')

        return await _dummy_session_stack(opened, closed, gen, call_tool)

    monkeypatch.setattr(McpClient, "_open_session", open_session)
    async with McpClient() as mcp:
        with pytest.raises(NetworkError):
            await mcp.call("submit", {}, retry=False)
        assert calls == [(1, "submit")] and closed == [1]
        # 之后的调用自动重连新会话
        assert (await mcp.call("list", {}))["ok"] is True

    assert opened == [1, 2] and closed == [1, 2]


@pytest.mark.anyio
async def test_non_retryable_mcp_error_keeps_session_and_auth_error_stops_immediately(monkeypatch):
    monkeypatch.setattr(client_mod, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(client_mod, "ensure_fresh", lambda store, http: SimpleNamespace(access_token="tok"))
    opened, closed = [], []

    async def open_session(self):
        gen = len(opened) + 1

        async def call_tool(tool, arguments, *, read_timeout_seconds):
            if tool == "bad":
                raise MCPError(types.ErrorData(code=types.INVALID_PARAMS, message="bad args"))
            if tool == "unauth":
                raise ExceptionGroup("eg", [AuthError("login_expired")])
            return result('{"head": {"code": 0}, "ok": true}')

        return await _dummy_session_stack(opened, closed, gen, call_tool)

    monkeypatch.setattr(McpClient, "_open_session", open_session)
    async with McpClient() as mcp:
        with pytest.raises(ToolError) as e:
            await mcp.call("bad", {})
        assert e.value.code == "mcp_error" and closed == []
        with pytest.raises(AuthError) as ae:
            await mcp.call("unauth", {})
        assert ae.value.code == "login_expired"
        assert (await mcp.call("ok", {}))["ok"] is True

    assert opened == [1] and closed == [1]


@pytest.mark.anyio
async def test_client_unwraps_body_exception_and_closes_session_cleanly(monkeypatch):
    monkeypatch.setattr(client_mod, "ensure_fresh", lambda store, http: SimpleNamespace(access_token="tok"))
    opened, closed = [], []

    async def open_session(self):
        return await _dummy_session_stack(opened, closed, len(opened) + 1, lambda *a, **kw: None)

    monkeypatch.setattr(McpClient, "_open_session", open_session)
    with pytest.raises(ValueError, match="boom"):
        async with McpClient():
            raise ValueError("boom")
    assert opened == [1] and closed == [1]
