"""场次与三类媒体子命令组：shots、keyframe、video、audio。"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from wrally.cli.base import (
    DirOpt,
    JsonOpt,
    ShotsOpt,
    VerboseOpt,
    _emit,
    _limit,
    handle_errors,
    refreshes_page,
    run_remote,
)
from wrally.commands import audio as audio_cmd
from wrally.commands import check as check_cmd
from wrally.commands import fetch as fetch_cmd
from wrally.commands import keyframe as keyframe_cmd
from wrally.commands import keyframe_gen as keyframe_gen_cmd
from wrally.commands import shots as shots_cmd
from wrally.commands import upscale as upscale_cmd
from wrally.commands import video as video_cmd
from wrally.commands import wait as wait_cmd
from wrally.errors import PreflightError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace


async def _gen_wait_fetch(
    ws: Workspace,
    client: McpClient,
    kind: str,
    submit: Callable[[], Awaitable[Any]],
    *,
    wait_: bool,
    fetch_: bool,
    timeout: float,
) -> tuple[Any, Any, Any]:
    """三类场次生成命令共用的 提交 → 可选等待 → 可选下载 串联。"""
    report = await submit()
    waited = fetched = None
    if report.submitted and (wait_ or fetch_):
        done = ",".join(report.submitted)
        waited = await wait_cmd.wait(ws, client, kind, done, timeout=_limit(timeout))
        if fetch_:
            fetched = await fetch_cmd.fetch(ws, client, kind, done)
    return report, waited, fetched


def register(app: typer.Typer) -> None:
    shots_app = typer.Typer(no_args_is_help=True, help="场次：rm 删除，order 按清单重排网页上的顺序")
    app.add_typer(shots_app, name="shots")

    @shots_app.command("rm")
    @handle_errors
    @refreshes_page
    def shots_rm(
        shots: Annotated[str, typer.Argument(help="要删的镜号，如 07 或 07,08、07-09；不接受 all")],
        dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会删哪些，不写远端")] = False,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """删除场次：远端进回收站，清单去掉这几镜，条目和提示词文件挪到 .wrally/removed/。"""
        ws = Workspace(directory)
        report = run_remote(verbose, lambda c: shots_cmd.rm(ws, c, shots, dry_run=dry_run))
        _emit(report.summary, shots_cmd.RM_HEADERS, report.rows, as_json, notes=tuple(report.notes))

    @shots_app.command("order")
    @handle_errors
    @refreshes_page
    def shots_order(
        by_key: Annotated[
            bool,
            typer.Option(
                "--by-key", help="先把清单按镜号排好（00 在前、99 在后）；会改成片顺序，先 --dry-run"
            ),
        ] = False,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会挪哪些，不写远端和清单")] = False,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """让网页上的场次顺序和清单一致。顺序以 episode.yaml 里条目的先后为准（export 也按它）。"""
        ws = Workspace(directory)
        report = run_remote(verbose, lambda c: shots_cmd.order(ws, c, by_key=by_key, dry_run=dry_run))
        _emit(
            report.summary,
            shots_cmd.ORDER_HEADERS,
            report.rows,
            as_json,
            details="episode.yaml",
            notes=tuple(report.notes),
        )

    keyframe_app = typer.Typer(no_args_is_help=True, help="关键帧：gen 生成候选、pick 选定、sheet 候选对比图")
    app.add_typer(keyframe_app, name="keyframe")

    @keyframe_app.command("gen")
    @handle_errors
    @refreshes_page
    def keyframe_gen(
        shots: ShotsOpt = "all",
        wait_: Annotated[bool, typer.Option("--wait", help="提交后等待完成")] = False,
        fetch_: Annotated[
            bool, typer.Option("--fetch", help="完成后下载候选并出对比图（隐含 --wait）")
        ] = False,
        redo: Annotated[bool, typer.Option("--redo", help="输入没变也再生成一批候选")] = False,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会提交哪些镜头，不调用远端")] = False,
        timeout: Annotated[float, typer.Option("--timeout", help="--wait 的最长等待秒数")] = 600,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """生成关键帧候选（消耗额度）。有 refs 的镜头走画布图生图。"""
        ws = Workspace(directory)
        report, waited, fetched = run_remote(
            verbose,
            lambda c: _gen_wait_fetch(
                ws,
                c,
                "keyframe",
                lambda: keyframe_gen_cmd.gen(ws, c, shots, redo=redo, dry_run=dry_run),
                wait_=wait_,
                fetch_=fetch_,
                timeout=timeout,
            ),
        )
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
        last: Annotated[
            bool, typer.Option("--last", help="选的是尾帧：这几镜的视频改走首尾帧模式；给 - 取消尾帧")
        ] = False,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
    ) -> None:
        """选定关键帧（或用 --last 选尾帧），一次可以选多镜。选哪张由人决定。"""
        if len(choices) % 2:
            raise PreflightError("bad_arguments", "(give pairs: <shot> <candidate>, e.g. 02 A 03 B)")
        pairs = list(zip(choices[::2], choices[1::2], strict=True))
        rows = keyframe_cmd.pick(Workspace(directory), pairs, last=last)
        noun = "last frames" if last else "keyframes"
        _emit(f"picked {len(rows)} {noun}  -> episode.yaml", keyframe_cmd.PICK_HEADERS, rows, as_json)

    @keyframe_app.command("sheet")
    @handle_errors
    def keyframe_sheet(
        shots: ShotsOpt = "all", directory: DirOpt = Path("."), as_json: JsonOpt = False
    ) -> None:
        """把各镜候选拼成对比图（每镜一张），已选的加绿框。"""
        rows, made = keyframe_cmd.sheet(Workspace(directory), shots)
        _emit(
            f"keyframe sheets: {made} written  -> .wrally/sheets/", keyframe_cmd.SHEET_HEADERS, rows, as_json
        )

    video_app = typer.Typer(no_args_is_help=True, help="视频：gen 按选定关键帧生成，upscale 超分")
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
        report, waited, fetched = run_remote(
            verbose,
            lambda c: _gen_wait_fetch(
                ws,
                c,
                "video",
                lambda: video_cmd.gen(ws, c, shots, redo=redo, force_picks=force_picks, dry_run=dry_run),
                wait_=wait_,
                fetch_=fetch_,
                timeout=timeout,
            ),
        )
        failed = report.failed
        if waited is None:
            _emit(report.summary, video_cmd.HEADERS, report.rows, as_json, details="episode.yaml")
        else:
            final = fetched or waited
            sheets = check_cmd.check(ws, ",".join(report.submitted)).sheets if fetched else 0
            tail = f"; {fetched.summary}; {sheets} sheets" if fetched else ""
            headers = fetched.headers if fetched else wait_cmd.HEADERS
            _emit(
                f"{report.summary}; {waited.summary}{tail}",
                headers,
                final.rows,
                as_json,
                details="episode.yaml",
            )
            failed += waited.failed + waited.timed_out + (fetched.failed if fetched else 0)
        if failed:
            raise typer.Exit(1)

    @video_app.command("upscale")
    @handle_errors
    @refreshes_page
    def video_upscale(
        shots: ShotsOpt = "all",
        model: Annotated[
            str | None,
            typer.Option("--model", help="超分模型 ID 或名称；默认取 defaults.upscale.model，再取远端默认"),
        ] = None,
        scale: Annotated[
            str | None,
            typer.Option("--scale", help="倍数，2 或 4；默认取 defaults.upscale.scale，再取远端默认"),
        ] = None,
        no_wait: Annotated[
            bool, typer.Option("--no-wait/--wait", help="--no-wait 只提交；默认提交后等待并下载，重跑接着等")
        ] = False,
        redo: Annotated[
            bool, typer.Option("--redo", help="即使输入没变（或上次提交超时）也重新提交")
        ] = False,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="只校验模型和倍数、列出会提交哪些镜头，不提交")
        ] = False,
        timeout: Annotated[float, typer.Option("--timeout", help="等待的最长秒数")] = 1200,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """给每镜现在用的视频做超分（消耗额度）。

        结果下载到 .wrally/video/<镜号>-x<倍数>.*，export 优先带它。
        """
        ws = Workspace(directory)

        async def work(client: McpClient) -> tuple:
            report = await upscale_cmd.upscale(
                ws, client, shots, model=model, scale=scale, redo=redo, dry_run=dry_run
            )
            if dry_run or no_wait:
                return report, None
            return report, await upscale_cmd.collect(ws, client, shots, timeout=_limit(timeout))

        report, collected = run_remote(verbose, work)
        if collected is None:
            _emit(report.summary, upscale_cmd.HEADERS, report.rows, as_json, details="episode.yaml")
            failed = report.failed
        else:
            # 提交出错的镜头不在等待结果里，并进表格，免得被埋掉
            errors = [r for r in report.rows if r[1].startswith("error")]
            rows = errors + [r for r in collected.rows if r[0] not in {e[0] for e in errors}]
            line = f"{report.summary}; {collected.waited}"
            _emit(line, upscale_cmd.HEADERS, rows, as_json, details="episode.yaml")
            failed = report.failed + collected.failed + collected.timed_out
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
            bool, typer.Option("--redo-failed", help="只重做质检未过（音高、声道差、底噪）或上次失败的镜头")
        ] = False,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出会提交哪些镜头，不调用远端")] = False,
        timeout: Annotated[float, typer.Option("--timeout", help="--wait 的最长等待秒数")] = 600,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """生成旁白（消耗额度）。输入没变的镜头不会重复提交。"""
        ws = Workspace(directory)
        report, waited, fetched = run_remote(
            verbose,
            lambda c: _gen_wait_fetch(
                ws,
                c,
                "audio",
                lambda: audio_cmd.gen(ws, c, shots, redo=redo, redo_failed=redo_failed, dry_run=dry_run),
                wait_=wait_,
                fetch_=fetch_,
                timeout=timeout,
            ),
        )
        failed = report.failed
        notes = tuple(report.notes)
        if waited is None:
            _emit(
                report.summary, audio_cmd.HEADERS, report.rows, as_json, details="episode.yaml", notes=notes
            )
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
