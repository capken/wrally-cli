"""命令行入口。只有这一层与人或 Claude 交互。"""

import functools
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import anyio
import httpx2
import typer

from wrally import output
from wrally.api.shots import shot_list
from wrally.commands import audio as audio_cmd
from wrally.commands import check as check_cmd
from wrally.commands import export as export_cmd
from wrally.commands import fetch as fetch_cmd
from wrally.commands import keyframe as keyframe_cmd
from wrally.commands import keyframe_gen as keyframe_gen_cmd
from wrally.commands import lint as lint_cmd
from wrally.commands import new as new_cmd
from wrally.commands import page as page_cmd
from wrally.commands import pull as pull_cmd
from wrally.commands import push as push_cmd
from wrally.commands import status as status_cmd
from wrally.commands import verify as verify_cmd
from wrally.commands import video as video_cmd
from wrally.commands import wait as wait_cmd
from wrally.config import mcp_url
from wrally.errors import PreflightError, WrallyError
from wrally.transport import auth
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="WorkRally 批量命令行工具",
)

JsonOpt = Annotated[bool, typer.Option("--json", help="输出机器可读的摘要")]
VerboseOpt = Annotated[bool, typer.Option("-v", "--verbose", help="打印每次远端调用的工具名和耗时")]
DirOpt = Annotated[Path, typer.Option("-C", "--dir", help="单集目录，默认当前目录")]


def _log(verbose: bool) -> Callable[[str], None] | None:
    return (lambda line: print(line, file=sys.stderr)) if verbose else None


def handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把错误收成一行错误码，并转换成约定的退出码。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except WrallyError as e:
            print(f"error: {e.code}  {e.hint}".rstrip(), file=sys.stderr)
            raise typer.Exit(e.exit_code) from e

    return wrapper


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


@app.command()
@handle_errors
def login(as_json: JsonOpt = False) -> None:
    """首次授权：打开浏览器登录 WorkRally，保存 token。"""
    with httpx2.Client(timeout=30) as http:
        creds = auth.login(
            auth.CredentialStore(), http, mcp_url(), notify=lambda line: print(line, file=sys.stderr)
        )
    info = auth.dump_public(creds)
    line = f"logged in  (token expires in {info['expires_in'] // 60} min, auto-refresh on)"
    print(output.render_json(line, **info) if as_json else line)


@app.command()
@handle_errors
@refreshes_page
def pull(
    project: Annotated[str | None, typer.Option("--project", help="短番项目 ID，首次 pull 必填")] = None,
    series: Annotated[str | None, typer.Option("--series", help="剧集 ID，首次 pull 必填")] = None,
    title: Annotated[str | None, typer.Option("--title", help="本集标题，写入清单")] = None,
    force: Annotated[bool, typer.Option("--force", help="本地提示词与远端不同时以远端为准")] = False,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """从 WorkRally 拉取场次列表，生成或更新 episode.yaml 与 prompts/。"""
    ws = Workspace(directory)
    existing = ws.load() or {}
    project_id = project or existing.get("project_id") or ws.project().get("project_id")
    series_id = series or existing.get("series_id")
    if not project_id or not series_id:
        raise PreflightError("missing_series", "(first pull needs --project and --series)")

    async def fetch() -> list:
        async with McpClient(log=_log(verbose)) as client:
            return await shot_list(client, series_id, project_id, raw_dir=ws.raw_dir)

    shots = anyio.run(fetch)
    report = pull_cmd.merge(ws, shots, project_id=project_id, series_id=series_id, title=title, force=force)
    line = pull_cmd.summary(report)
    rows = report.table()
    details = str(ws.state_dir / "pull.json")
    if as_json:
        print(output.render_json(line, pull_cmd.HEADERS, rows, details=details))
    else:
        print(output.render(line, pull_cmd.HEADERS, rows, details=details, notes=report.notes))


ShotsOpt = Annotated[str, typer.Option("--shots", help="镜头范围：all、02,05、02-07")]
KindArg = Annotated[str, typer.Argument(help="keyframe | video | audio")]


def _kind(kind: str) -> str:
    if kind not in fetch_cmd.KINDS:
        raise PreflightError("unknown_kind", f"({kind}; use keyframe, video or audio)")
    return kind


def _emit(line: str, headers: tuple[str, ...], rows: list, as_json: bool, *, details: str | None = None,
          notes: tuple[str, ...] = ()) -> None:  # fmt: skip
    if as_json:
        print(output.render_json(line, headers, rows, details=details))
    else:
        print(output.render(line, headers, rows, details=details, notes=notes))


def _remote(verbose: bool, work: Callable[[McpClient], Any]) -> Any:
    """在一个 MCP 会话里跑完一条命令的全部远端调用。"""

    async def run() -> Any:
        async with McpClient(log=_log(verbose)) as client:
            return await work(client)

    return anyio.run(run)


@app.command()
@handle_errors
@refreshes_page
def new(
    shots: Annotated[str, typer.Option("--shots", help="要建的镜号，如 00-11,99（00 片头，99 片尾）")],
    project: Annotated[str | None, typer.Option("--project", help="短番项目 ID，清单里没有时必填")] = None,
    name: Annotated[str | None, typer.Option("--name", help="新剧集的名称，如「EP4 信仰与理性」")] = None,
    series: Annotated[str | None, typer.Option("--series", help="沿用已有的剧集，只补场次")] = None,
    title: Annotated[str | None, typer.Option("--title", help="本集标题，写入清单")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会建什么，不写远端")] = False,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """新建剧集和场次并建好清单；清单里已有剧集时只补缺的场次。"""
    ws = Workspace(directory)
    report = _remote(
        verbose,
        lambda c: new_cmd.new(
            ws, c, shots, project_id=project, name=name, series_id=series, title=title, dry_run=dry_run
        ),
    )
    notes = ()
    if report.added and not dry_run:
        notes = (
            "note: new shots carry placeholder prompts; edit prompts/, `wrally lint`, then `wrally push`",
        )
    _emit(report.summary, new_cmd.HEADERS, report.rows, as_json, details="episode.yaml", notes=notes)


@app.command()
@handle_errors
def status(directory: DirOpt = Path("."), as_json: JsonOpt = False) -> None:
    """打印全集进度表。只读清单，不调用远端。

    在项目目录（有 wrally.yaml、没有 episode.yaml）下运行时每集一行。
    """
    ws = Workspace(directory)
    if not ws.manifest_path.exists() and (directory / "wrally.yaml").is_file():
        line, rows = status_cmd.project_status(directory)
        _emit(line, status_cmd.PROJECT_HEADERS, rows, as_json)
        return
    line, rows = status_cmd.status(ws)
    _emit(line, status_cmd.HEADERS, rows, as_json, details="episode.yaml")


@app.command()
@handle_errors
@refreshes_page
def fetch(kind: KindArg, shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False,
          verbose: VerboseOpt = False) -> None:  # fmt: skip
    """下载最新结果到 .wrally/；已下载的不重复下载。"""
    ws = Workspace(directory)
    kind = _kind(kind)
    report = _remote(verbose, lambda c: fetch_cmd.fetch(ws, c, kind, shots))
    _emit(report.summary, report.headers, report.rows, as_json, details="episode.yaml")
    if report.failed:
        raise typer.Exit(1)


@app.command()
@handle_errors
@refreshes_page
def wait(
    kind: KindArg,
    shots: ShotsOpt = "all",
    timeout: Annotated[float, typer.Option("--timeout", help="最长等待秒数")] = 1200,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """等待任务完成，只在结束时输出一次结果。"""
    ws = Workspace(directory)
    kind = _kind(kind)
    report = _remote(verbose, lambda c: wait_cmd.wait(ws, c, kind, shots, timeout=timeout))
    _emit(report.summary, wait_cmd.HEADERS, report.rows, as_json, details="episode.yaml")
    if report.failed or report.timed_out:
        raise typer.Exit(1)


@app.command()
@handle_errors
@refreshes_page
def push(
    what: Annotated[str, typer.Option("--what", help="prompts | audio-prompt")] = "prompts",
    shots: ShotsOpt = "all",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只对比，不写远端")] = False,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """把本地提示词写回 WorkRally；只写与远端不同的镜头。"""
    ws = Workspace(directory)
    report = _remote(verbose, lambda c: push_cmd.push(ws, c, what, shots, dry_run))
    _emit(
        report.summary,
        report.headers,
        report.rows,
        as_json,
        details=".wrally/raw/",
        notes=tuple(report.notes),
    )
    if report.failed:
        raise typer.Exit(1)


keyframe_app = typer.Typer(no_args_is_help=True, help="关键帧：pick 选定、sheet 候选对比图")
app.add_typer(keyframe_app, name="keyframe")


@keyframe_app.command("gen")
@handle_errors
@refreshes_page
def keyframe_gen(
    shots: ShotsOpt = "all",
    wait_: Annotated[bool, typer.Option("--wait", help="提交后等待完成")] = False,
    fetch_: Annotated[bool, typer.Option("--fetch", help="完成后下载候选并出对比图（隐含 --wait）")] = False,
    redo: Annotated[bool, typer.Option("--redo", help="输入没变也再生成一批候选")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会提交哪些镜头，不调用远端")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="--wait 的最长等待秒数")] = 600,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """生成关键帧候选（消耗额度）。有 refs 的镜头走画布图生图。"""
    ws = Workspace(directory)

    async def work(client: McpClient) -> tuple:
        report = await keyframe_gen_cmd.gen(ws, client, shots, redo=redo, dry_run=dry_run)
        waited = fetched = None
        if report.submitted and (wait_ or fetch_):
            done = ",".join(report.submitted)
            waited = await wait_cmd.wait(ws, client, "keyframe", done, timeout=timeout)
            if fetch_:
                fetched = await fetch_cmd.fetch(ws, client, "keyframe", done)
        return report, waited, fetched

    report, waited, fetched = _remote(verbose, work)
    failed = report.failed
    if waited is None:
        _emit(report.summary, keyframe_gen_cmd.HEADERS, report.rows, as_json, details="episode.yaml")
    elif fetched is None:
        _emit(
            f"{report.summary}; {waited.summary}",
            wait_cmd.HEADERS,
            waited.rows,
            as_json,
            details="episode.yaml",
        )
        failed += waited.failed + waited.timed_out
    else:
        rows, made = keyframe_cmd.sheet(ws, ",".join(report.submitted))
        line = f"{report.summary}; {waited.summary}; {fetched.summary}; {made} sheets"
        _emit(line, keyframe_cmd.SHEET_HEADERS, rows, as_json, details="episode.yaml")
        failed += waited.failed + waited.timed_out + fetched.failed
    if failed:
        raise typer.Exit(1)


@keyframe_app.command("pick")
@handle_errors
@refreshes_page
def keyframe_pick(
    choices: Annotated[
        list[str], typer.Argument(help="成对给出：镜号 候选序号，如 02 A 03 B；也可以直接给 asset_id")
    ],
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
) -> None:
    """选定关键帧，一次可以选多镜。选哪张由人决定。"""
    if len(choices) % 2:
        raise PreflightError("bad_arguments", "(give pairs: <shot> <candidate>, e.g. 02 A 03 B)")
    rows = keyframe_cmd.pick(Workspace(directory), list(zip(choices[::2], choices[1::2], strict=True)))
    _emit(f"picked {len(rows)} keyframes  -> episode.yaml", keyframe_cmd.PICK_HEADERS, rows, as_json)


@keyframe_app.command("sheet")
@handle_errors
def keyframe_sheet(shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False) -> None:
    """把各镜候选拼成对比图（每镜一张），已选的加绿框。"""
    rows, made = keyframe_cmd.sheet(Workspace(directory), shots)
    _emit(f"keyframe sheets: {made} written  -> .wrally/sheets/", keyframe_cmd.SHEET_HEADERS, rows, as_json)


video_app = typer.Typer(no_args_is_help=True, help="视频：gen 按选定关键帧生成")
app.add_typer(video_app, name="video")


@video_app.command("gen")
@handle_errors
@refreshes_page
def video_gen(
    shots: ShotsOpt = "all",
    wait_: Annotated[bool, typer.Option("--wait", help="提交后等待完成")] = False,
    fetch_: Annotated[bool, typer.Option("--fetch", help="完成后下载并抽帧（隐含 --wait）")] = False,
    redo: Annotated[bool, typer.Option("--redo", help="即使输入没变也重新生成")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会提交哪些镜头，不调用远端")] = False,
    force_picks: Annotated[
        bool, typer.Option("--force-picks", help="网页选用与清单冲突时以清单为准")
    ] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="--wait 的最长等待秒数")] = 1200,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """按选定关键帧生成视频（消耗额度）。输入没变的镜头不会重复提交。"""
    ws = Workspace(directory)

    async def work(client: McpClient) -> tuple:
        report = await video_cmd.gen(ws, client, shots, redo=redo, force_picks=force_picks, dry_run=dry_run)
        waited = fetched = None
        if report.submitted and (wait_ or fetch_):
            done = ",".join(report.submitted)
            waited = await wait_cmd.wait(ws, client, "video", done, timeout=timeout)
            if fetch_:
                fetched = await fetch_cmd.fetch(ws, client, "video", done)
        return report, waited, fetched

    report, waited, fetched = _remote(verbose, work)
    failed = report.failed
    if waited is None:
        _emit(report.summary, video_cmd.HEADERS, report.rows, as_json, details="episode.yaml")
    else:
        final = fetched or waited
        sheets = check_cmd.check(ws, ",".join(report.submitted)).sheets if fetched else 0
        tail = f"; {fetched.summary}; {sheets} sheets" if fetched else ""
        headers = fetched.headers if fetched else wait_cmd.HEADERS
        _emit(
            f"{report.summary}; {waited.summary}{tail}", headers, final.rows, as_json, details="episode.yaml"
        )
        failed += waited.failed + waited.timed_out + (fetched.failed if fetched else 0)
    if failed:
        raise typer.Exit(1)


audio_app = typer.Typer(no_args_is_help=True, help="旁白：gen 按台词生成")
app.add_typer(audio_app, name="audio")


@audio_app.command("gen")
@handle_errors
@refreshes_page
def audio_gen(
    shots: ShotsOpt = "all",
    wait_: Annotated[bool, typer.Option("--wait", help="提交后等待完成")] = False,
    fetch_: Annotated[bool, typer.Option("--fetch", help="完成后下载并检查音高（隐含 --wait）")] = False,
    redo: Annotated[bool, typer.Option("--redo", help="即使输入没变也重新生成")] = False,
    redo_failed: Annotated[
        bool, typer.Option("--redo-failed", help="只重做音高超标或上次失败的镜头")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会提交哪些镜头，不调用远端")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="--wait 的最长等待秒数")] = 600,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """生成旁白（消耗额度）。输入没变的镜头不会重复提交。"""
    ws = Workspace(directory)

    async def work(client: McpClient) -> tuple:
        report = await audio_cmd.gen(ws, client, shots, redo=redo, redo_failed=redo_failed, dry_run=dry_run)
        waited = fetched = None
        if report.submitted and (wait_ or fetch_):
            done = ",".join(report.submitted)
            waited = await wait_cmd.wait(ws, client, "audio", done, timeout=timeout)
            if fetch_:
                fetched = await fetch_cmd.fetch(ws, client, "audio", done)
        return report, waited, fetched

    report, waited, fetched = _remote(verbose, work)
    failed = report.failed
    notes = tuple(report.notes)
    if waited is None:
        _emit(report.summary, audio_cmd.HEADERS, report.rows, as_json, details="episode.yaml", notes=notes)
    elif fetched is None:
        line = f"{report.summary}; {waited.summary}"
        _emit(line, wait_cmd.HEADERS, waited.rows, as_json, details="episode.yaml", notes=notes)
        failed += waited.failed + waited.timed_out
    else:
        checked = check_cmd.check(ws, ",".join(report.submitted))
        line = f"{report.summary}; {waited.summary}; {fetched.summary}; {checked.warnings} warnings"
        _emit(line, check_cmd.HEADERS, checked.rows, as_json, details=checked.report_path, notes=notes)
        failed += waited.failed + waited.timed_out + fetched.failed
    if failed:
        raise typer.Exit(1)


@app.command()
@handle_errors
def lint(shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False) -> None:
    """检查本地提示词和台词；只列有问题的镜头。有错误时退出码为 2。"""
    report = lint_cmd.lint(Workspace(directory), shots)
    _emit(report.summary, lint_cmd.HEADERS, report.rows, as_json, details=".wrally/lint.md")
    if report.errors:
        raise typer.Exit(2)


@app.command("verify-picks")
@handle_errors
def verify_picks(shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False,
                 verbose: VerboseOpt = False) -> None:  # fmt: skip
    """核对清单选图与网页「选用」是否一致；不一致时退出码为 2。"""
    ws = Workspace(directory)
    report = _remote(verbose, lambda c: verify_cmd.verify_picks(ws, c, shots))
    _emit(report.summary, verify_cmd.HEADERS, report.rows, as_json, details=".wrally/verify/")
    if report.mismatches:
        raise typer.Exit(2)
    if report.errors:
        raise typer.Exit(1)


@app.command()
@handle_errors
@refreshes_page
def check(shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False) -> None:
    """生成抽帧拼图和检查报告；终端只列有警告的镜头。"""
    report = check_cmd.check(Workspace(directory), shots)
    _emit(report.summary, check_cmd.HEADERS, report.flagged(), as_json, details=report.report_path,
          notes=tuple(report.notes))  # fmt: skip


@app.command()
@handle_errors
def export(shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False) -> None:
    """把合成需要的输入写成 .wrally/export.json：每镜的视频、旁白、台词和时长。不调用远端。

    清单和项目文件里 wrally 不认识的键原样带出。终端只列素材缺失或过期的镜头，照常导出。
    """
    report = export_cmd.export(Workspace(directory), shots)
    _emit(report.summary, export_cmd.HEADERS, report.rows, as_json, details=report.path)


@app.command()
@handle_errors
def page(
    watch: Annotated[bool, typer.Option("--watch", help="守着工作区，文件一变就重建，直到 Ctrl-C")] = False,
    open_: Annotated[bool, typer.Option("--open", help="建好后用浏览器打开")] = False,
    directory: DirOpt = Path("."),
    as_json: JsonOpt = False,
) -> None:
    """把当前进展做成网页 page.html：每镜的关键帧、视频、旁白和提示词。不调用远端。

    在项目目录（有 wrally.yaml、没有 episode.yaml）下运行时重建每一集的页面和项目总览页 index.html。
    """
    ws = Workspace(directory)
    if not ws.manifest_path.exists() and (directory / "wrally.yaml").is_file():
        if watch:
            raise PreflightError("bad_arguments", "(--watch works in an episode directory)")
        report = page_cmd.build_index(directory, rebuild=True)
    else:
        report = page_cmd.build(ws)
    _emit(report.summary, (), [], as_json)
    if open_:
        webbrowser.open((ws.root / report.path).resolve().as_uri())
    if watch:
        print("watching for changes, Ctrl-C to stop", file=sys.stderr)
        try:
            page_cmd.watch(directory)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    app()
