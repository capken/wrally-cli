import http.client
import shutil
import threading

import pytest
from conftest import entry

from wrally.commands import page, serve
from wrally.errors import PreflightError
from wrally.workspace import Workspace


def project_with(ws, tmp_path_factory, names=("ep1", "ep2")):
    root = tmp_path_factory.mktemp("project")
    (root / "wrally.yaml").write_text("title: 示例片\n", encoding="utf-8")
    for name in names:
        shutil.copytree(ws.root, root / name)
    return root


@pytest.fixture
def running():
    """起一个服务（系统挑端口），返回 (site, 发请求的函数)；测试结束时关掉。"""
    servers = []

    def start(directory):
        site = serve.locate(directory)
        serve.build_all(site)
        server = serve.bind(site, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)

        def get(path, headers=None, method="GET"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request(method, path, headers=headers or {})
            res = conn.getresponse()
            body = res.read()
            conn.close()
            return res, body

        return site, get

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_locate(ws, tmp_path_factory):
    root = project_with(ws, tmp_path_factory)
    site = serve.locate(root)
    assert site.root == root.resolve() and site.start == "/index.html"
    assert [f.name for f in site.episodes] == ["ep1", "ep2"]
    # 在某一集里运行：根目录照样是项目目录，打开时直接到这一集
    site = serve.locate(root / "ep2")
    assert site.root == root.resolve() and site.start == "/ep2/page.html" and site.project
    # 不属于项目的单集只服务它自己
    site = serve.locate(ws.root)
    assert site.root == ws.root.resolve() and site.start == "/page.html" and not site.project
    with pytest.raises(PreflightError) as e:
        serve.locate(tmp_path_factory.mktemp("empty"))
    assert e.value.code == "no_manifest"


def test_parse_range():
    assert serve._parse_range("bytes=0-9", 100) == (0, 9)
    assert serve._parse_range("bytes=90-", 100) == (90, 99)
    assert serve._parse_range("bytes=-10", 100) == (90, 99)
    assert serve._parse_range("bytes=95-200", 100) == (95, 99)
    for bad in ("bytes=100-", "bytes=5-2", "bytes=0-1,4-5", "items=0-1", "bytes=x-", "bytes=-0"):
        assert serve._parse_range(bad, 100) is None, bad
    assert serve._parse_range("bytes=0-", 0) is None


def test_serves_pages_and_media(ws, tmp_path_factory, running):
    root = project_with(ws, tmp_path_factory)
    (root / "ep1" / ".wrally" / "video").mkdir(parents=True)
    (root / "ep1" / ".wrally" / "video" / "02.mp4").write_bytes(bytes(range(256)) * 4)
    _, get = running(root)
    assert (root / "index.html").is_file() and (root / "ep2" / "page.html").is_file()  # 启动时都建好了

    res, _ = get("/")
    assert res.status == 302 and res.getheader("Location") == "/index.html"
    res, body = get("/index.html")
    assert res.status == 200 and "示例片" in body.decode() and res.getheader("Cache-Control") == "no-cache"
    res, _ = get("/ep1/page.html")
    assert res.status == 200
    assert get("/ep1/")[0].status == 404  # 不列目录
    assert get("/nope.html")[0].status == 404

    res, body = get("/ep1/.wrally/video/02.mp4", {"Range": "bytes=10-19"})
    assert res.status == 206 and body == bytes(range(10, 20))
    assert res.getheader("Content-Range") == "bytes 10-19/1024"
    assert res.getheader("Content-Type") == "video/mp4"
    res, body = get("/ep1/.wrally/video/02.mp4")
    assert res.status == 200 and len(body) == 1024 and res.getheader("Cache-Control") is None
    assert res.getheader("Accept-Ranges") == "bytes"
    res, _ = get("/ep1/.wrally/video/02.mp4", method="HEAD")
    assert res.status == 200 and res.getheader("Accept-Ranges") == "bytes"
    assert get("/ep1/.wrally/video/02.mp4", {"Range": "bytes=2000-"})[0].status == 416


def test_request_rebuilds_a_changed_episode(ws, tmp_path_factory, running):
    root = project_with(ws, tmp_path_factory)
    _, get = running(root)
    ep1 = Workspace(root / "ep1")
    _, before = get("/ep1/.wrally/page.version.js")
    _, index_before = get("/.wrally/index.version.js")
    ep1.write_text(entry(ep1, "02")["image_prompt"], "服务开着时改的一句")
    _, after = get("/ep1/.wrally/page.version.js")
    assert after != before  # 开着的页面靠它发现更新
    assert "服务开着时改的一句" in get("/ep1/page.html")[1].decode()
    # 没再改：不重建
    stamp = (root / "ep1" / "page.html").stat().st_mtime_ns
    get("/ep1/page.html")
    assert (root / "ep1" / "page.html").stat().st_mtime_ns == stamp
    # 只改了提示词，总览页上的数没变
    assert get("/.wrally/index.version.js")[1] == index_before
    # 选了图：总览页的数跟着变
    (root / "ep1" / ".wrally" / "keyframes" / "01").mkdir(parents=True)
    (root / "ep1" / ".wrally" / "keyframes" / "01" / "pick-k1.png").write_bytes(b"x")
    data = ep1.load()
    next(e for e in data["shots"] if e["key"] == "01")["keyframe"] = {"asset_id": "k1"}
    ep1.save(data)
    assert get("/.wrally/index.version.js")[1] != index_before


def test_broken_manifest_keeps_the_last_page(ws, tmp_path_factory, running, capsys):
    root = project_with(ws, tmp_path_factory)
    _, get = running(root)
    old = (root / "ep1" / "page.html").read_bytes()
    (root / "ep1" / "episode.yaml").write_text("shots: [", encoding="utf-8")
    res, body = get("/ep1/page.html")
    assert res.status == 200 and body == old
    get("/ep1/page.html")
    assert capsys.readouterr().err.count("ep1: page not rebuilt") == 1


def test_standalone_episode(ws, running):
    site, get = running(ws.root)
    res, _ = get("/")
    assert res.status == 302 and res.getheader("Location") == "/page.html"
    assert get("/page.html")[0].status == 200 and not (ws.root / page.INDEX).exists()


def test_port_in_use_moves_on(ws):
    site = serve.locate(ws.root)
    first = serve.bind(site, "127.0.0.1", 0)
    try:
        port = first.server_address[1]
        second = serve.bind(site, "127.0.0.1", port)
        assert second.server_address[1] != port
        second.server_close()
        assert serve.url(first, site) == f"http://localhost:{port}/page.html"
    finally:
        first.server_close()


def test_client_reset_is_not_printed(ws, capfd):
    """浏览器开了连接又掐掉（RST）：不往终端打 traceback，服务照常。"""
    import socket
    import struct
    import time

    site = serve.locate(ws.root)
    serve.build_all(site)
    server = serve.bind(site, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sock = socket.create_connection(server.server_address[:2])
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close 时发 RST
        sock.send(b"GET /page.html HTTP/1.1\r\n")  # 请求没发完，服务端还在读
        time.sleep(0.1)
        sock.close()
        time.sleep(0.2)
        assert "Traceback" not in capfd.readouterr().err
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/page.html")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_discovers_new_episode_while_running(ws, tmp_path_factory, running):
    """服务开着期间新建一集：不用重启，总览页和那一集的页面都能直接访问。"""
    root = project_with(ws, tmp_path_factory, names=("ep1",))
    _, get = running(root)
    assert "ep2" not in get("/index.html")[1].decode()
    shutil.copytree(ws.root, root / "ep2")
    assert "ep2" in get("/index.html")[1].decode()
    assert get("/ep2/page.html")[0].status == 200
    assert (root / "ep2" / "page.html").is_file()
