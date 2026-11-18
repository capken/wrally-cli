"""serve：在本机起一个只读的网页服务，用浏览器看项目总览页和各集的进度网页。不调用远端。

在项目目录下运行时根目录是项目目录，首页是 index.html；在单集目录下运行时，这一集属于某个项目
（项目文件就在上一级）就照样以项目目录为根，打开时直接到这一集，否则只服务这一集。

页面本来就会定时加载 .wrally/page.version.js 来发现更新。服务在收到页面或版本文件的请求时，先看这一集
的文件有没有变（page.signature，只看修改时间），变了才重建，所以手改了提示词或清单，开着的页面也会自己刷新。
视频要能拖动进度，所以支持 Range 请求（标准库的 SimpleHTTPRequestHandler 不支持）。
"""

import os
import socket
import sys
import threading
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from wrally.commands import page as page_cmd
from wrally.errors import PreflightError
from wrally.workspace import PROJECT, Workspace

HOST = "127.0.0.1"
PORT = 8765
TRIES = 20  # 端口被占用时往后试几个
CHUNK = 64 * 1024


@dataclass
class Site:
    """服务的根目录和其中的各集。episodes 为空时根目录本身就是一集。"""

    root: Path
    start: str  # 打开时的路径，如 /index.html、/ep3/page.html
    episodes: list[Path] = field(default_factory=list)
    seen: dict[Path, tuple] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    warned: set[str] = field(default_factory=set)

    @property
    def project(self) -> bool:
        return bool(self.episodes)


def locate(directory: Path) -> Site:
    directory = directory.resolve()
    ws = Workspace(directory)
    if ws.manifest_path.is_file():
        project = ws.project_path()
        if project and project.parent == directory.parent:
            root = project.parent
            return Site(root, f"/{quote(directory.name)}/{page_cmd.PAGE}", page_cmd.episode_dirs(root))
        return Site(directory, f"/{page_cmd.PAGE}")
    if (directory / PROJECT).is_file():
        return Site(directory, f"/{page_cmd.INDEX}", page_cmd.episode_dirs(directory))
    raise PreflightError("no_manifest", "(run in a project or episode directory)")


def _refresh(site: Site, folder: Path) -> bool:
    """这一集的文件变了就重建它的页面（不带总览页）。返回是否重建过。"""
    ws = Workspace(folder)
    now = page_cmd.signature(ws)
    if site.seen.get(folder) == now:
        return False
    site.seen[folder] = now
    try:
        page_cmd.build(ws, index=False)
    except Exception as e:  # noqa: BLE001
        # 清单正改到一半之类：照旧给出上次的页面，同一个错误只提示一次
        message = f"warn: {folder.name}: page not rebuilt ({type(e).__name__})"
        if message not in site.warned:
            site.warned.add(message)
            print(message, file=sys.stderr)
        return False
    return True


def refresh(site: Site, path: str) -> None:
    """按请求的路径重建需要的页面：某一集的页面或版本文件只看那一集；总览页要看每一集。"""
    parts = [p for p in path.split("/") if p]
    tail = "/".join(parts[-2:]) if len(parts) > 1 else "/".join(parts)
    with site.lock:
        if not site.project:
            if tail in (page_cmd.PAGE, f".wrally/{page_cmd.VERSION}"):
                _refresh(site, site.root)
            return
        if parts in ([], [page_cmd.INDEX], [".wrally", page_cmd.INDEX_VERSION]):
            changed = [_refresh(site, folder) for folder in site.episodes]
            if any(changed) or not (site.root / page_cmd.INDEX).is_file():
                page_cmd.build_index(site.root)
            return
        folder = next((f for f in site.episodes if parts[0] == f.name), None)
        if folder and parts[1:] in ([page_cmd.PAGE], [".wrally", page_cmd.VERSION]):
            if _refresh(site, folder):
                page_cmd.build_index(site.root)


def build_all(site: Site) -> None:
    """启动时建一遍，并记下各集的文件状态。"""
    for folder in site.episodes or [site.root]:
        site.seen.pop(folder, None)
        _refresh(site, folder)
    if site.project:
        page_cmd.build_index(site.root)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 保持连接：一页上几十张图，不用每张都重新连
    site: Site

    def __init__(self, *args: Any, site: Site, **kwargs: Any) -> None:
        self.site = site
        super().__init__(*args, directory=str(site.root), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # 终端只留启动时那一行

    def end_headers(self) -> None:
        # 页面和版本文件每次都要新的；素材地址带 ?v=修改时间，可以缓存
        if urlsplit(self.path).path.endswith((".html", ".js")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _route(self) -> str | None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/{page_cmd.INDEX}" if self.site.project else self.site.start)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        refresh(self.site, path)
        return path

    def do_GET(self) -> None:  # noqa: N802
        if self._route() is None:
            return
        target = Path(self.translate_path(self.path))
        if target.is_dir():
            self.send_error(HTTPStatus.NOT_FOUND)  # 不列目录
            return
        if "Range" in self.headers and target.is_file():
            self._send_range(target)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._route() is None:
            return
        if Path(self.translate_path(self.path)).is_dir():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_HEAD()

    def _send_range(self, target: Path) -> None:
        size = target.stat().st_size
        span = _parse_range(self.headers["Range"], size)
        if span is None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = span
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(target)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with target.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 浏览器拖动进度时会掐掉上一个请求


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """`bytes=a-b`、`bytes=a-`、`bytes=-n` → (起, 止)，都含；只支持一段。不合法或越界返回 None。"""
    unit, _, spec = header.partition("=")
    if unit.strip() != "bytes" or "," in spec or size == 0:
        return None
    first, _, last = spec.strip().partition("-")
    try:
        if not first:
            length = int(last)
            if length <= 0:
                return None
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # 浏览器会预先开几条连接，用不上就掐掉；标准库会把这种断开整段 traceback 打到终端
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def bind(site: Site, host: str, port: int) -> ThreadingHTTPServer:
    """从 port 开始找一个空闲的端口。port 为 0 时由系统挑。"""
    handler = partial(Handler, site=site)
    last: OSError | None = None
    for candidate in [port] if port == 0 else range(port, port + TRIES):
        try:
            return Server((host, candidate), handler)
        except OSError as e:
            last = e
    raise PreflightError("port_in_use", f"({host}:{port}-{port + TRIES - 1}: {last}; use --port)")


def url(server: ThreadingHTTPServer, site: Site) -> str:
    host, port = server.server_address[:2]
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    if shown in ("0.0.0.0", "::"):  # noqa: S104
        shown = socket.gethostname()
    return f"http://{shown}:{port}{site.start}"


def describe(site: Site) -> str:
    where = str(site.root)
    home = str(Path.home())
    if where == home or where.startswith(home + os.sep):
        where = "~" + where[len(home) :]
    if not site.project:
        return f"{where} (one episode)"
    count = len(site.episodes)
    return f"{where} ({count} episode{'s' if count != 1 else ''})"
