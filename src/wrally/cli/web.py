"""本地进度网页命令：page、serve。"""

import signal
import sys
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from wrally.cli.base import DirOpt, JsonOpt, _emit, handle_errors
from wrally.commands import page as page_cmd
from wrally.commands import serve as serve_cmd
from wrally.errors import PreflightError
from wrally.workspace import Workspace


def register(app: typer.Typer) -> None:
    @app.command()
    @handle_errors
    def page(
        watch: Annotated[
            bool, typer.Option("--watch", help="守着工作区，文件一变就重建，直到 Ctrl-C")
        ] = False,
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
        _emit(report.summary, (), [], as_json, data=report.data)
        if open_:
            webbrowser.open((ws.root / report.path).resolve().as_uri())
        if watch:
            print("watching for changes, Ctrl-C to stop", file=sys.stderr)
            try:
                page_cmd.watch(directory)
            except KeyboardInterrupt:
                pass

    @app.command()
    @handle_errors
    def serve(
        port: Annotated[
            int, typer.Option("--port", help="端口，被占用时往后找；0 由系统挑")
        ] = serve_cmd.PORT,
        host: Annotated[str, typer.Option("--host", help="监听地址；默认只有本机能访问")] = serve_cmd.HOST,
        open_: Annotated[bool, typer.Option("--open", help="起来后用浏览器打开")] = False,
        directory: DirOpt = Path("."),
        as_json: JsonOpt = False,
    ) -> None:
        """起一个本机网页服务，在浏览器里看项目总览页和各集的进度网页，直到 Ctrl-C。不调用远端。

        在项目目录或单集目录下运行。文件变了，请求页面时自动重建，开着的页面会自己刷新。
        """
        site = serve_cmd.locate(directory)
        serve_cmd.build_all(site)
        server = serve_cmd.bind(site, host, port)
        address = serve_cmd.url(server, site)
        _emit(
            f"serving {serve_cmd.describe(site)}  -> {address}  (Ctrl-C to stop)",
            (),
            [],
            as_json,
            data={"url": address, "root": str(site.root)},
        )
        sys.stdout.flush()
        if open_:
            webbrowser.open(address)

        def stop(*_: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)  # kill 和 Ctrl-C 一样正常退出
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
