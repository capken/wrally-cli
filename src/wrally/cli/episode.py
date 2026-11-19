"""单集主流程命令：login、pull、new、status、fetch、wait、push、lint、verify-picks、check、export。"""

import sys
from pathlib import Path
from typing import Annotated

import httpx2
import typer

from wrally import output
from wrally.api.shots import shot_list
from wrally.cli.base import (
    DirOpt,
    JsonOpt,
    KindArg,
    ShotsOpt,
    VerboseOpt,
    _emit,
    _kind,
    _limit,
    handle_errors,
    refreshes_page,
    run_remote,
)
from wrally.commands import check as check_cmd
from wrally.commands import export as export_cmd
from wrally.commands import fetch as fetch_cmd
from wrally.commands import lint as lint_cmd
from wrally.commands import new as new_cmd
from wrally.commands import pull as pull_cmd
from wrally.commands import push as push_cmd
from wrally.commands import status as status_cmd
from wrally.commands import verify as verify_cmd
from wrally.commands import wait as wait_cmd
from wrally.config import mcp_url
from wrally.errors import PreflightError
from wrally.transport import auth
from wrally.workspace import Workspace


def register_core(app: typer.Typer) -> None:
    """注册前半段单集基础命令（login / pull / new / status / fetch / wait / push）。"""

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

        shots = run_remote(
            verbose, lambda client: shot_list(client, series_id, project_id, raw_dir=ws.raw_dir)
        )
        report = pull_cmd.merge(
            ws, shots, project_id=project_id, series_id=series_id, title=title, force=force
        )
        _emit(
            pull_cmd.summary(report),
            pull_cmd.HEADERS,
            report.table(),
            as_json,
            details=str(ws.state_dir / "pull.json"),
            notes=tuple(report.notes),
        )

    @app.command()
    @handle_errors
    @refreshes_page
    def new(
        shots: Annotated[str, typer.Option("--shots", help="要建的镜号，如 00-11,99（00 片头，99 片尾）")],
        project: Annotated[
            str | None, typer.Option("--project", help="短番项目 ID，清单里没有时必填")
        ] = None,
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
        report = run_remote(
            verbose,
            lambda c: new_cmd.new(
                ws, c, shots, project_id=project, name=name, series_id=series, title=title, dry_run=dry_run
            ),
        )
        notes = []
        if report.placeholders:
            notes.append(
                "note: new shots carry placeholder prompts; edit prompts/, `wrally lint`, then `wrally push`"
            )
        if report.kept_local:
            notes.append(
                "note: local prompts were kept over the placeholders; `wrally lint`, then `wrally push`"
            )
        _emit(
            report.summary, new_cmd.HEADERS, report.rows, as_json, details="episode.yaml", notes=tuple(notes)
        )

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
    def fetch(
        kind: KindArg,
        shots: ShotsOpt = "all",
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """下载最新结果到 .wrally/；已下载的不重复下载。"""
        ws = Workspace(directory)
        kind = _kind(kind)
        report = run_remote(verbose, lambda c: fetch_cmd.fetch(ws, c, kind, shots))
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
        report = run_remote(verbose, lambda c: wait_cmd.wait(ws, c, kind, shots, timeout=_limit(timeout)))
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
        report = run_remote(verbose, lambda c: push_cmd.push(ws, c, what, shots, dry_run))
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


def register_checks(app: typer.Typer) -> None:
    """注册质检与导出命令（lint / verify-picks / check / export）。"""

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
    def verify_picks(
        shots: ShotsOpt = "all",
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """核对清单选图与网页「选用」是否一致；不一致时退出码为 2。"""
        ws = Workspace(directory)
        report = run_remote(verbose, lambda c: verify_cmd.verify_picks(ws, c, shots))
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
        _emit(
            report.summary,
            check_cmd.HEADERS,
            report.flagged(),
            as_json,
            details=report.report_path,
            notes=tuple(report.notes),
            data=report.data,
        )

    @app.command()
    @handle_errors
    def export(shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False) -> None:
        """把合成需要的输入写成 .wrally/export.json：每镜的视频、旁白、台词和时长。不调用远端。

        清单和项目文件里 wrally 不认识的键原样带出。终端只列素材缺失或过期的镜头，照常导出。
        """
        report = export_cmd.export(Workspace(directory), shots)
        _emit(report.summary, export_cmd.HEADERS, report.rows, as_json, details=report.path)
