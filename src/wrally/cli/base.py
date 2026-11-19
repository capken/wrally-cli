"""CLI 公共基础设施：选项类型、统一错误处理、页面自动刷新、摘要输出与远端会话。"""

import functools
import json
import re
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import anyio
import click
import typer

from wrally import output
from wrally.commands import fetch as fetch_cmd
from wrally.commands import page as page_cmd
from wrally.config import config_dir, max_wait
from wrally.errors import INTERNAL_EXIT_CODE, PreflightError, WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

JsonOpt = Annotated[bool, typer.Option("--json", help="输出机器可读的摘要")]
VerboseOpt = Annotated[bool, typer.Option("-v", "--verbose", help="打印每次远端调用的工具名和耗时")]
DirOpt = Annotated[Path, typer.Option("-C", "--dir", help="单集目录，默认当前目录")]
ShotsOpt = Annotated[str, typer.Option("--shots", help="镜头范围：all、02,05、02-07")]
KindArg = Annotated[str, typer.Argument(help="keyframe | video | audio")]

# typer 自己的退出和用法错误、Ctrl-C 照常往外抛
_PASS_THROUGH = (typer.Exit, typer.Abort, click.ClickException, SystemExit, KeyboardInterrupt)


def _log(verbose: bool) -> Callable[[str], None] | None:
    return (lambda line: print(line, file=sys.stderr)) if verbose else None


def _fail(code: str, hint: str, exit_code: int, as_json: bool) -> None:
    """错误收成一行写到标准错误；--json 时标准输出再给一行 JSON，解析标准输出的调用方也拿得到。"""
    print(f"error: {code}  {hint}".rstrip(), file=sys.stderr)
    if as_json:
        print(json.dumps({"error": code, "hint": hint, "exit_code": exit_code}, ensure_ascii=False))
    raise typer.Exit(exit_code)


def _find_wrally_error(exc: BaseException) -> WrallyError | None:
    """WrallyError 可能被包在 ExceptionGroup 里（任务组、并发执行）。"""
    if isinstance(exc, WrallyError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            if found := _find_wrally_error(inner):
                return found
    return None


def _save_crash(exc: BaseException) -> Path | None:
    """追踪栈写到凭证目录（不在工作区，不会被提交）；终端只给路径。"""
    try:
        path = config_dir() / "last-crash.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(traceback.format_exception(exc))
        path.write_text(re.sub(r"https?://\S+", "<url>", text), encoding="utf-8")
        return path
    except OSError:
        return None


def handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把错误收成一行错误码，并转换成约定的退出码；意外的异常也不打追踪栈，退出码 4。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        as_json = bool(kwargs.get("as_json"))
        try:
            return fn(*args, **kwargs)
        except _PASS_THROUGH:
            raise
        except BaseException as e:  # noqa: BLE001  CancelledError、ExceptionGroup 也在这里收
            if found := _find_wrally_error(e):
                _fail(found.code, found.hint, found.exit_code, as_json)
            path = _save_crash(e)
            hint = f"{type(e).__name__} (traceback: {path})" if path else type(e).__name__
            _fail("internal_error", hint, INTERNAL_EXIT_CODE, as_json)

    return wrapper


def _limit(timeout: float) -> float:
    """WRALLY_MAX_WAIT 压住所有等待时长。"""
    cap = max_wait()
    return min(timeout, cap) if cap else timeout


def refreshes_page(fn: Callable[..., Any]) -> Callable[..., Any]:
    """命令结束后顺带重建 page.html，成功、部分失败、出错都建：清单可能已经改了。

    不往标准输出写任何东西，也不改变原命令的退出码；建不出来只在标准错误里留一行。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        finally:
            try:
                ws = Workspace(kwargs.get("directory") or Path("."))
                if page_cmd.enabled(ws):
                    page_cmd.build(ws)
            except Exception as e:  # noqa: BLE001
                code = e.code if isinstance(e, WrallyError) else type(e).__name__
                print(f"warn: page not updated  {code}", file=sys.stderr)

    return wrapper


def _kind(kind: str) -> str:
    if kind not in fetch_cmd.KINDS:
        raise PreflightError("unknown_kind", f"({kind}; use keyframe, video or audio)")
    return kind


def _emit(
    line: str,
    headers: tuple[str, ...],
    rows: list,
    as_json: bool,
    *,
    details: str | None = None,
    notes: tuple[str, ...] = (),
    data: dict | None = None,
) -> None:
    if as_json:
        extra: dict[str, Any] = {"details": details}
        if data is not None:
            extra["data"] = data
        print(output.render_json(line, headers, rows, **extra))
    else:
        print(output.render(line, headers, rows, details=details, notes=notes))


def _remote(verbose: bool, work: Callable[[McpClient], Any]) -> Any:
    """在一个 MCP 会话里跑完一条命令的全部远端调用。"""

    async def run() -> Any:
        async with McpClient(log=_log(verbose)) as client:
            return await work(client)

    return anyio.run(run)


def run_remote(verbose: bool, work: Callable[[McpClient], Any]) -> Any:
    """经由 wrally.cli._remote 调用，便于测试在包入口上统一 monkeypatch。"""
    import wrally.cli as root_cli

    return root_cli._remote(verbose, work)
