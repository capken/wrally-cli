"""单独生成与项目工具命令：gen（image/video/audio/music/fetch）、smoke、models、upload、projects。"""

from pathlib import Path
from typing import Annotated

import typer

from wrally import output
from wrally.api import canvas as canvas_api
from wrally.cli.base import JsonOpt, VerboseOpt, _emit, _limit, handle_errors, run_remote
from wrally.commands import gen as gen_cmd
from wrally.commands import projects as projects_cmd
from wrally.commands import smoke as smoke_cmd
from wrally.commands import upload as upload_cmd
from wrally.errors import PreflightError
from wrally.transport.client import McpClient

PromptArg = Annotated[str | None, typer.Argument(help="提示词；长的用 --prompt-file")]
PromptFileOpt = Annotated[Path | None, typer.Option("--prompt-file", help="从文件读提示词")]
ModelOpt = Annotated[str, typer.Option("--model", help="模型 ID 或名称，见 `wrally models <类别>`")]
CountOpt = Annotated[int, typer.Option("--count", help="生成几份，1-4")]
OutOpt = Annotated[Path, typer.Option("-o", "--out", help="输出目录；提交记录也存在这里")]
NameOpt = Annotated[str | None, typer.Option("--name", help="文件名（不含扩展名），默认按类别和参数指纹取")]
ProjectOpt = Annotated[
    str | None,
    typer.Option("--project", envvar="WRALLY_PROJECT", help="短番项目 ID：结果归到这个项目的媒资库（可选）"),
]
# 默认就等待并下载；--wait 也收下（和场次生成命令的 --wait 同名，Agent 常顺手加上），效果同默认
NoWaitOpt = Annotated[
    bool,
    typer.Option(
        "--no-wait/--wait", help="--no-wait 只提交，之后用 gen fetch；默认（或 --wait）提交后等待并下载"
    ),
]
TimeoutOpt = Annotated[float, typer.Option("--timeout", help="等待的最长秒数")]
AgainOpt = Annotated[bool, typer.Option("--again", help="同样的参数已经提交过也再提交一批")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="只校验参数、说明会提交什么，不提交")]


def _prompt(prompt: str | None, prompt_file: Path | None) -> str:
    if prompt_file is not None:
        if not prompt_file.is_file():
            raise PreflightError("missing_file", f"({prompt_file})")
        return prompt_file.read_text(encoding="utf-8").strip()
    return (prompt or "").strip()


def _gen(
    kind: str,
    params: dict,
    out: Path,
    project: str | None,
    no_wait: bool,
    timeout: float,
    again: bool,
    dry_run: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    async def work(client: McpClient) -> tuple:
        report = await gen_cmd.gen(
            client, kind, params, out, project_id=project, again=again, dry_run=dry_run
        )
        if dry_run or no_wait or not report.job_ids:
            return report, None
        return report, await gen_cmd.fetch(client, out, job_ids=report.job_ids, timeout=_limit(timeout))

    report, fetched = run_remote(verbose, work)
    if fetched is None:
        _emit(report.summary, (), [], as_json)
        return
    details = str(out / gen_cmd.LOG)
    _emit(f"{report.summary}; {fetched.summary}", gen_cmd.HEADERS, fetched.rows, as_json, details=details)
    if fetched.failed or fetched.timed_out:
        raise typer.Exit(1)


def _call_gen(
    kind: str,
    params: dict,
    out: Path,
    project: str | None,
    no_wait: bool,
    timeout: float,
    again: bool,
    dry_run: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """经由 wrally.cli._gen 调用，便于测试在包入口上统一 monkeypatch。"""
    import wrally.cli as root_cli

    root_cli._gen(kind, params, out, project, no_wait, timeout, again, dry_run, as_json, verbose)


def register(app: typer.Typer) -> None:
    gen_app = typer.Typer(
        no_args_is_help=True,
        help="单独生成图片、视频、音频、音乐（消耗额度），不用建项目和清单，结果直接下载成文件",
    )
    app.add_typer(gen_app, name="gen")

    @gen_app.command("image")
    @handle_errors
    def gen_image(
        prompt: PromptArg = None,
        model: ModelOpt = ...,
        prompt_file: PromptFileOpt = None,
        aspect: Annotated[str | None, typer.Option("--aspect", help="宽高比，如 16:9")] = None,
        resolution: Annotated[
            int | None, typer.Option("--resolution", help="分辨率枚举：4=1K、5=2K、6=4K")
        ] = None,
        count: CountOpt = 1,
        ref: Annotated[
            list[str] | None,
            typer.Option("--ref", help="参考图：媒资 ID 或本地图片，可多次；提示词里用「第一张图片」引用"),
        ] = None,
        out: OutOpt = Path("."),
        name: NameOpt = None,
        project: ProjectOpt = None,
        no_wait: NoWaitOpt = False,
        timeout: TimeoutOpt = 600,
        again: AgainOpt = False,
        dry_run: DryRunOpt = False,
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """生成图片（消耗额度）。"""
        params = {
            "prompt": _prompt(prompt, prompt_file),
            "model": model,
            "count": count,
            "aspect": aspect,
            "resolution": resolution,
            "refs": ref or [],
            "name": name,
        }
        _call_gen("image", params, out, project, no_wait, timeout, again, dry_run, as_json, verbose)

    @gen_app.command("video")
    @handle_errors
    def gen_video(
        prompt: PromptArg = None,
        model: ModelOpt = ...,
        prompt_file: PromptFileOpt = None,
        mode: Annotated[
            str, typer.Option("--mode", help="text（文字，可带一张图）或 firstlast（首尾帧）")
        ] = "text",
        image: Annotated[str | None, typer.Option("--image", help="单图或首帧：媒资 ID 或本地图片")] = None,
        last: Annotated[
            str | None, typer.Option("--last", help="尾帧：媒资 ID 或本地图片（firstlast）")
        ] = None,
        duration: Annotated[int | None, typer.Option("--duration", help="时长（秒）")] = None,
        resolution: Annotated[
            int | None, typer.Option("--resolution", help="分辨率枚举：1=480p、3=720p、4=1080p")
        ] = None,
        aspect: Annotated[str | None, typer.Option("--aspect", help="宽高比，如 16:9")] = None,
        sound: Annotated[bool, typer.Option("--sound", help="同时生成音效（默认不生成）")] = False,
        count: CountOpt = 1,
        out: OutOpt = Path("."),
        name: NameOpt = None,
        project: ProjectOpt = None,
        no_wait: NoWaitOpt = False,
        timeout: TimeoutOpt = 1200,
        again: AgainOpt = False,
        dry_run: DryRunOpt = False,
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """生成视频（消耗额度）。"""
        if mode not in ("text", "firstlast"):
            raise PreflightError("bad_mode", f"({mode}; allowed: text, firstlast)")
        params = {
            "prompt": _prompt(prompt, prompt_file),
            "model": model,
            "count": count,
            "mode": mode,
            "image": image,
            "last": last,
            "duration": duration,
            "resolution": resolution,
            "aspect": aspect,
            "sound": sound,
            "name": name,
        }
        _call_gen("video", params, out, project, no_wait, timeout, again, dry_run, as_json, verbose)

    @gen_app.command("audio")
    @handle_errors
    def gen_audio(
        prompt: PromptArg = None,
        model: ModelOpt = ...,
        prompt_file: PromptFileOpt = None,
        voice: Annotated[
            list[str] | None,
            typer.Option("--voice", help="参考音色：媒资 ID 或本地音频，可多次（MiniMax 模型不接受）"),
        ] = None,
        preset: Annotated[
            str | None, typer.Option("--preset", help="预置音色 ID，只用于 MiniMax 模型（如 badao_shaoye）")
        ] = None,
        field: Annotated[
            list[str] | None,
            typer.Option("--field", help="模型参数 名=数值，可多次（如 audio_speech_rate=15）"),
        ] = None,
        count: CountOpt = 1,
        out: OutOpt = Path("."),
        name: NameOpt = None,
        project: ProjectOpt = None,
        no_wait: NoWaitOpt = False,
        timeout: TimeoutOpt = 600,
        again: AgainOpt = False,
        dry_run: DryRunOpt = False,
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """生成配音或音效（消耗额度）。提示词里写台词和说话方式。"""
        params = {
            "prompt": _prompt(prompt, prompt_file),
            "model": model,
            "count": count,
            "voices": voice or [],
            "name": name,
        }
        if preset:  # 不用预置音色时不写这个键：保持旧提交记录的指纹不变
            params["preset"] = preset
        if field:
            fields = {}
            for item in field:
                field_name, sep, value = item.partition("=")
                if not sep or not field_name.strip():
                    raise PreflightError("bad_field", f"({item!r}; use name=value)")
                value = value.strip()
                fields[field_name.strip()] = int(value) if value.lstrip("-").isdigit() else value
            params["fields"] = fields
        _call_gen("audio", params, out, project, no_wait, timeout, again, dry_run, as_json, verbose)

    @gen_app.command("music")
    @handle_errors
    def gen_music(
        prompt: PromptArg = None,
        model: ModelOpt = ...,
        prompt_file: PromptFileOpt = None,
        lyrics_file: Annotated[
            Path | None, typer.Option("--lyrics-file", help="歌词文件；不给则是纯器乐")
        ] = None,
        ai_lyrics: Annotated[bool, typer.Option("--ai-lyrics", help="让 AI 写歌词")] = False,
        count: CountOpt = 1,
        out: OutOpt = Path("."),
        name: NameOpt = None,
        project: ProjectOpt = None,
        no_wait: NoWaitOpt = False,
        timeout: TimeoutOpt = 600,
        again: AgainOpt = False,
        dry_run: DryRunOpt = False,
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """生成音乐（消耗额度）。提示词写风格和配器。"""
        if lyrics_file is not None and ai_lyrics:
            raise PreflightError("bad_arguments", "(--lyrics-file and --ai-lyrics are exclusive)")
        lyrics = "ai" if ai_lyrics else (_prompt(None, lyrics_file) if lyrics_file else None)
        params = {
            "prompt": _prompt(prompt, prompt_file),
            "model": model,
            "count": count,
            "lyrics": lyrics,
            "name": name,
        }
        _call_gen("music", params, out, project, no_wait, timeout, again, dry_run, as_json, verbose)

    @gen_app.command("fetch")
    @handle_errors
    def gen_fetch(
        out: OutOpt = Path("."),
        job: Annotated[
            list[str] | None, typer.Option("--job", help="只处理这些记录，如 003；默认所有没下载完的")
        ] = None,
        timeout: TimeoutOpt = 600,
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """等待输出目录里提交过的任务并下载结果；提交超时、--no-wait 或下载中断后用它接上。"""
        report = run_remote(
            verbose, lambda client: gen_cmd.fetch(client, out, job_ids=job, timeout=_limit(timeout))
        )
        _emit(report.summary, gen_cmd.HEADERS, report.rows, as_json, details=str(out / gen_cmd.LOG))
        if report.failed or report.timed_out:
            raise typer.Exit(1)

    @app.command()
    @handle_errors
    def smoke(
        image_model: Annotated[
            str | None,
            typer.Option("--image-model", help="图像模型；默认 defaults.image.model，没有则用内置的"),
        ] = None,
        audio_model: Annotated[
            str | None,
            typer.Option("--audio-model", help="音频模型；默认 defaults.audio.model，没有则用内置的"),
        ] = None,
        video_model: Annotated[
            str | None,
            typer.Option("--video-model", help="视频模型；默认 defaults.video.model，没有则用内置的"),
        ] = None,
        only: Annotated[str | None, typer.Option("--only", help="只跑这几步，如 image,audio")] = None,
        project: ProjectOpt = None,
        resume: Annotated[
            bool, typer.Option("--resume", help="接着最近一次运行：已提交的不重复提交")
        ] = False,
        timeout: TimeoutOpt = 1800,
        dry_run: DryRunOpt = False,
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """真实调用 WorkRally，依次生成图像、配音和 5 秒视频并检查文件（消耗额度）。

        结果在当前目录的 .wrally-smoke/ 下。
        """
        models_given = {"image": image_model, "audio": audio_model, "video": video_model}
        steps = [s.strip() for s in only.split(",") if s.strip()] if only else None
        report = run_remote(
            verbose,
            lambda client: smoke_cmd.run(
                client,
                Path("."),
                models=models_given,
                only=steps,
                project_id=project,
                resume=resume,
                dry_run=dry_run,
                timeout=_limit(timeout),
            ),
        )
        details = str(report.run_dir / smoke_cmd.PLAN) if report.run_dir else None
        _emit(report.summary, smoke_cmd.HEADERS, report.rows, as_json, details=details)
        if report.failed or report.pending:
            raise typer.Exit(1)

    @app.command()
    @handle_errors
    def models(
        kind: Annotated[str, typer.Argument(help="image、video、audio 或 music")],
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """列出 gen 可用的模型和参数范围。"""
        if kind not in canvas_api.KINDS:
            raise PreflightError("bad_kind", f"({kind}; allowed: {', '.join(canvas_api.KINDS)})")
        found = run_remote(verbose, lambda client: canvas_api.models(client, kind))

        def span(values: list) -> str:
            return ",".join(str(v) for v in values) if len(values) <= 6 else f"{values[0]}-{values[-1]}"

        if kind == "image":
            headers = ("id", "name", "resolution", "aspect")
            rows = [(m.id, m.name, span(m.resolutions), span(m.aspects)) for m in found]
        elif kind == "video":
            headers = ("id", "name", "modes", "seconds", "resolution", "sound")
            rows = [
                (
                    m.id,
                    m.name,
                    ",".join(m.modes),
                    span(m.durations),
                    span(m.resolutions),
                    "yes" if m.sound else "-",
                )
                for m in found
            ]
        else:
            headers = ("id", "name", "voices")
            rows = [
                (m.id, m.name, "-" if m.minimax or not m.max_refs else f"up to {m.max_refs}") for m in found
            ]
        line = f"{len(rows)} {kind} models"
        if as_json:
            print(output.render_json(line, headers, rows))
        else:  # 模型清单本身就是要看的内容，不按 15 行截断
            print(output.render(line, headers, rows, max_rows=max(len(rows), output.MAX_TABLE_ROWS)))

    @app.command()
    @handle_errors
    def upload(
        files: Annotated[list[str], typer.Argument(help="本地图片、视频或音频文件")],
        project: Annotated[
            str | None,
            typer.Option("--project", help="归到哪个项目的媒资库；默认取 -C 目录的项目，再取「默认项目」"),
        ] = None,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="只检查文件、列出哪些要上传")] = False,
        directory: Annotated[
            Path, typer.Option("-C", "--dir", help="按这个目录（单集或项目目录）定项目，默认当前目录")
        ] = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """把本地文件上传成媒资，打印媒资 ID。同一个文件在同一个项目里只传一次。

        gen 的 --ref/--image/--last/--voice、清单的 ref_assets 和 defaults.audio.voice_asset
        也能直接写本地路径，用到时自动上传；这条命令用来先传好、拿到 ID。
        """
        line, rows = run_remote(
            verbose,
            lambda c: upload_cmd.run(c, files, project_id=project, directory=directory, dry_run=dry_run),
        )
        _emit(line, upload_cmd.HEADERS, rows, as_json)

    projects_app = typer.Typer(help="短番项目：不带子命令时列出全部项目；create 新建")
    app.add_typer(projects_app, name="projects")

    @projects_app.callback(invoke_without_command=True)
    @handle_errors
    def projects(
        ctx: typer.Context,
        search: Annotated[str | None, typer.Option("--search", help="按名称模糊搜索")] = None,
        trash: Annotated[bool, typer.Option("--trash", help="列回收站里的项目")] = False,
        directory: Annotated[
            Path, typer.Option("-C", "--dir", help="按这个目录（项目或单集目录）标出当前项目，默认当前目录")
        ] = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """列出 WorkRally 上的全部短番项目和各有几集；当前目录所属的项目标 *。"""
        if ctx.invoked_subcommand:
            return
        line, rows = run_remote(
            verbose, lambda c: projects_cmd.listing(c, directory, search=search, trash=trash)
        )
        headers = projects_cmd.LIST_HEADERS
        if as_json:
            print(output.render_json(line, headers, rows))
        else:  # 项目清单本身就是要看的内容，不按 15 行截断
            print(output.render(line, headers, rows, max_rows=max(len(rows), output.MAX_TABLE_ROWS)))

    @projects_app.command("create")
    @handle_errors
    def projects_create(
        name: Annotated[str, typer.Argument(help="项目名称")],
        canvas: Annotated[
            str | None, typer.Option("--canvas", help="画布尺寸 宽:高，如 1920:1080（不给用远端默认）")
        ] = None,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="只检查重名和目录，不建")] = False,
        directory: Annotated[
            Path, typer.Option("-C", "--dir", help="项目目录（放 wrally.yaml、各集在它下面），默认当前目录")
        ] = Path("."),
        as_json: JsonOpt = False,
        verbose: VerboseOpt = False,
    ) -> None:
        """新建短番项目（不自动建剧集），并在项目目录写出 wrally.yaml；之后在子目录里 `wrally new` 建各集。"""
        report = run_remote(
            verbose, lambda c: projects_cmd.create(c, directory, name, canvas=canvas, dry_run=dry_run)
        )
        _emit(report.summary, projects_cmd.CREATE_HEADERS, report.rows, as_json, notes=tuple(report.notes))
