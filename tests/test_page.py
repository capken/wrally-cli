import json

from conftest import entry
from typer.testing import CliRunner

from wrally.cli import app
from wrally.commands import page
from wrally.commands.common import flow_seq


def put(ws, rel: str, content: bytes = b"x") -> None:
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def edit(ws, key: str, change) -> None:
    data = ws.load()
    change(next(e for e in data["shots"] if e["key"] == key))
    ws.save(data)


def shot(ws, key: str) -> dict:
    return next(s for s in page.collect(ws)["shots"] if s["key"] == key)


def embedded(ws) -> dict:
    html = (ws.root / "page.html").read_text(encoding="utf-8")
    start = html.index('type="application/json">') + len('type="application/json">')
    return json.loads(html[start : html.index("</script>", start)])


def test_build_is_idempotent_and_writes_version_file(ws):
    first = page.build(ws)
    assert first.changed and first.summary == "page: 4 shots  -> page.html  (updated)"
    version = (ws.state_dir / "page.version.js").read_text(encoding="utf-8")
    before = (ws.root / "page.html").stat().st_mtime_ns
    again = page.build(ws)
    assert not again.changed and again.summary.endswith("(unchanged)")
    assert (ws.root / "page.html").stat().st_mtime_ns == before
    assert (ws.state_dir / "page.version.js").read_text(encoding="utf-8") == version
    assert [s["key"] for s in embedded(ws)["shots"]] == ["00", "01", "02", "99"]


def test_version_changes_when_a_prompt_changes(ws):
    page.build(ws)
    version = (ws.state_dir / "page.version.js").read_text(encoding="utf-8")
    rel = entry(ws, "02")["image_prompt"]
    ws.write_text(rel, "换了一句话")
    assert page.build(ws).changed
    assert (ws.state_dir / "page.version.js").read_text(encoding="utf-8") != version
    assert shot(ws, "02")["prompts"]["image"]["own"] == "换了一句话"


def test_media_states(ws):
    def change(e):
        e["video"] = {"status": "done", "file": ".wrally/video/02.mp4"}
        e["audio"] = {"status": "running"}

    edit(ws, "02", change)
    s = shot(ws, "02")
    assert s["video"]["status"]["state"] == "missing" and s["video"]["src"] is None
    assert s["audio"]["status"]["state"] == "running"
    put(ws, ".wrally/video/02.mp4")
    s = shot(ws, "02")
    assert s["video"]["status"]["state"] == "done"
    assert s["video"]["src"].startswith(".wrally/video/02.mp4?v=")
    assert shot(ws, "01")["video"]["status"]["state"] == "none"


def test_keyframe_pick_candidates_and_skip(ws):
    def change(e):
        e["keyframe"] = {"candidates": flow_seq(["a1", "a2"]), "asset_id": "a2", "pick": "B"}

    edit(ws, "02", change)
    assert shot(ws, "02")["keyframe"]["status"]["state"] == "missing"
    put(ws, ".wrally/keyframes/02/A.png")
    put(ws, ".wrally/keyframes/02/B.png")
    kf = shot(ws, "02")["keyframe"]
    assert kf["status"]["state"] == "done" and kf["pick"] == "B"
    assert kf["src"].startswith(".wrally/keyframes/02/B.png?v=")
    assert [(c["label"], c["picked"]) for c in kf["candidates"]] == [("A", False), ("B", True)]

    edit(ws, "02", lambda e: e["keyframe"].pop("asset_id"))
    assert shot(ws, "02")["keyframe"]["status"]["state"] == "pending"

    # 不在候选里的选用图存成 pick-<asset_id>
    edit(ws, "01", lambda e: e.update(keyframe={"asset_id": "zz"}))
    put(ws, ".wrally/keyframes/01/pick-zz.png")
    kf = shot(ws, "01")["keyframe"]
    assert kf["status"]["state"] == "done" and kf["pick"] == "" and "pick-zz.png" in kf["src"]

    def skip(e):
        e.pop("keyframe", None)
        e["skip"] = flow_seq(["keyframe"])

    edit(ws, "00", skip)
    assert shot(ws, "00")["keyframe"]["status"]["state"] == "skipped"


def test_stale_video_is_flagged(ws):
    put(ws, ".wrally/video/02.mp4")

    def change(e):
        e.setdefault("keyframe", {})["asset_id"] = "key02"
        e["video"] = {"status": "done", "file": ".wrally/video/02.mp4", "made_from": "not-this"}

    edit(ws, "02", change)
    data = ws.load()
    data["defaults"] = {"video": {"model": "vid1"}}
    ws.save(data)
    status = shot(ws, "02")["video"]["status"]
    assert status["state"] == "stale" and status["note"].startswith("video was made")


def test_prompt_is_split_into_own_and_common_text(ws):
    ws.write_text("styles/suffix.md", "公共风格。")
    ws.write_text("prompts/02.image.md", "一个人提着灯")
    data = ws.load()
    data["defaults"] = {"style_suffix": "styles/suffix.md"}
    for e in data["shots"]:
        e.pop("raw", None)
        if e["key"] == "02":
            e["image_prompt"] = "prompts/02.image.md"
    ws.save(data)
    p = shot(ws, "02")["prompts"]["image"]
    assert (p["before"], p["own"], p["after"], p["raw"]) == ("", "一个人提着灯", "。公共风格。", False)

    edit(ws, "02", lambda e: e.update(raw=flow_seq(["image"])))
    p = shot(ws, "02")["prompts"]["image"]
    assert p["raw"] and p["own"] == "一个人提着灯" and p["after"] == ""

    # 公共文本的文件没了：页面照样能出，只是标出来
    (ws.root / "styles/suffix.md").unlink()
    found = page.collect(ws)
    assert found["note"].startswith("style_file_missing")
    assert next(s for s in found["shots"] if s["key"] == "02")["prompts"]["image"]["own"] == "一个人提着灯"


def test_prompt_text_cannot_break_out_of_the_page(ws):
    rel = entry(ws, "02")["image_prompt"]
    nasty = "</script><img src=x onerror=alert(1)> https://example.com/a.png?sig=1"
    ws.write_text(rel, nasty)
    page.build(ws)
    html = (ws.root / "page.html").read_text(encoding="utf-8")
    assert "</script><img" not in html and "<img src=x" not in html
    assert embedded(ws)["shots"][2]["prompts"]["image"]["own"].startswith("</script><img")


def test_film_is_linked_when_present(ws):
    assert page.collect(ws)["film"] is None
    data = ws.load()
    data["output"] = "EP 成片.mp4"
    ws.save(data)
    put(ws, "EP 成片.mp4")
    assert page.collect(ws)["film"].startswith("EP%20%E6%88%90%E7%89%87.mp4?v=")


def test_enabled_can_be_switched_off(ws, tmp_path):
    assert page.enabled(ws)
    data = ws.load()
    data["page"] = False
    ws.save(data)
    assert not page.enabled(ws)
    from wrally.workspace import Workspace

    assert not page.enabled(Workspace(tmp_path / "empty"))


def test_watch_rebuilds_only_when_files_change(ws):
    page.build(ws)
    rel = entry(ws, "02")["image_prompt"]
    ticks = []

    def sleep(_):
        ticks.append(1)
        if len(ticks) == 2:
            ws.write_text(rel, "第二版")

    assert page.watch(ws.root, sleep=sleep, rounds=4) == 1
    assert "第二版" in (ws.root / "page.html").read_text(encoding="utf-8")


def test_cli_page_prints_one_line(ws):
    result = CliRunner().invoke(app, ["page", "-C", str(ws.root)])
    assert result.exit_code == 0 and result.output.strip() == "page: 4 shots  -> page.html  (updated)"
    as_json = CliRunner().invoke(app, ["page", "-C", str(ws.root), "--json"])
    assert json.loads(as_json.output)["summary"].endswith("(unchanged)")


def test_other_commands_refresh_the_page_quietly(ws):
    put(ws, ".wrally/keyframes/02/A.png")
    edit(ws, "02", lambda e: e.update(keyframe={"candidates": flow_seq(["a1"])}))
    result = CliRunner().invoke(app, ["keyframe", "pick", "02", "A", "-C", str(ws.root)])
    assert result.exit_code == 0 and "page" not in result.output
    assert embedded(ws)["shots"][2]["keyframe"]["pick"] == "A"

    # 关掉之后不再重建
    data = ws.load()
    data["page"] = False
    ws.save(data)
    (ws.root / "page.html").unlink()
    CliRunner().invoke(app, ["check", "-C", str(ws.root)])
    assert not (ws.root / "page.html").exists()


def test_failed_command_keeps_its_exit_code(ws):
    result = CliRunner().invoke(app, ["keyframe", "pick", "77", "A", "-C", str(ws.root)])
    assert result.exit_code == 2
    assert (ws.root / "page.html").exists()


def test_prompts_edited_on_the_web_are_marked(ws, remote, stories):
    from conftest import set_web_copy

    from wrally.api.models import Shot
    from wrally.commands.common import remember_web_copies
    from wrally.prompts import composed, load_styles

    # 还没调过远端：没有记录，什么都不标
    assert shot(ws, "01")["prompts"]["image"]["web"] is None

    data = ws.load()
    styles = load_styles(ws, data)
    full = {e["key"]: composed(ws, styles, e, "image") for e in data["shots"]}
    set_web_copy(stories[1], full["01"])  # 01：网页副本和本地一致
    set_web_copy(stories[2], "网页上的旧文本")  # 02：不一致
    set_web_copy(stories[2], "网页上的旧动效", "animation_prompt_json")
    remember_web_copies(ws, [Shot.from_story(s) for s in stories])

    assert shot(ws, "01")["prompts"]["image"]["web"] == "same"
    assert shot(ws, "01")["prompts"]["anim"]["web"] is None
    assert shot(ws, "02")["prompts"]["image"]["web"] == "differs"
    assert shot(ws, "02")["prompts"]["anim"]["web"] == "differs"
    assert shot(ws, "00")["prompts"]["image"]["web"] is None

    # 本地改了提示词，不用再调远端页面就知道两份不一样了
    ws.write_text(entry(ws, "01")["image_prompt"], "本地新写的")
    assert shot(ws, "01")["prompts"]["image"]["web"] == "differs"

    # 用户在网页上粘贴好之后，下一次拿到场次列表就清掉
    remember_web_copies(ws, [Shot.from_story(s) for s in [stories[0], stories[3]]])
    assert shot(ws, "02")["prompts"]["image"]["web"] is None


def test_pull_records_web_copies(ws, stories):
    from conftest import set_web_copy

    from wrally.api.models import Shot
    from wrally.commands.common import web_copies
    from wrally.commands.pull import merge

    assert web_copies(ws) == {}
    set_web_copy(stories[1], "网页上的文本")
    merge(ws, [Shot.from_story(s) for s in stories], project_id="proj", series_id="ser")
    assert web_copies(ws) == {stories[1]["story_id"]: {"image": "网页上的文本"}}


def project_with(ws, tmp_path_factory, names=("ep1", "ep2")):
    """一个带项目文件的目录，里面每一集都是 ws 的拷贝。"""
    import shutil

    from wrally.workspace import Workspace

    root = tmp_path_factory.mktemp("project")
    (root / "wrally.yaml").write_text("title: 示例片\n", encoding="utf-8")
    for name in names:
        shutil.copytree(ws.root, root / name)
    return root, [Workspace(root / name) for name in names]


def index_data(root) -> dict:
    html = (root / "index.html").read_text(encoding="utf-8")
    start = html.index('type="application/json">') + len('type="application/json">')
    return json.loads(html[start : html.index("</script>", start)])


def test_episode_build_also_builds_the_project_index(ws, tmp_path_factory):
    root, (ep1, ep2) = project_with(ws, tmp_path_factory)
    put(ep1, ".wrally/keyframes/01/pick-k1.png")
    put(ep1, ".wrally/keyframes/02/pick-k2.png")
    edit(ep1, "01", lambda e: e.update(keyframe={"asset_id": "k1"}))
    edit(ep1, "02", lambda e: e.update(keyframe={"asset_id": "k2"}))
    page.build(ep1)

    found = index_data(root)
    assert found["title"] == "示例片"
    # ep2 还没建过页面：总览页顺带把它建出来
    assert [e["name"] for e in found["episodes"]] == ["ep1", "ep2"] and (ep2.root / "page.html").exists()
    first = found["episodes"][0]
    assert first["href"] == "ep1/page.html" and first["shots"] == 4 and first["open"] == 4
    assert first["stats"]["keyframe"] == [2, 4] and not first["film"]
    assert first["cover"].startswith("ep1/.wrally/keyframes/02/pick-k2.png?v=")  # 封面取第二张

    # 单集页面里有回总览页的入口和各集的切换
    assert embedded(ep1)["index"] == "../index.html" and embedded(ep1)["episodes"] == ["ep1", "ep2"]
    assert not (ws.root / "index.html").exists() and page.collect(ws)["index"] is None  # 没有项目文件的不建

    before = (root / "index.html").stat().st_mtime_ns
    page.build(ep1)
    assert (root / "index.html").stat().st_mtime_ns == before


def test_cli_page_in_project_directory_rebuilds_everything(ws, tmp_path_factory):
    root, (ep1, _) = project_with(ws, tmp_path_factory)
    (root / "notes").mkdir()  # 没有清单的目录不算一集
    result = CliRunner().invoke(app, ["page", "-C", str(root)])
    assert result.exit_code == 0 and result.output.strip() == "page: 2 episodes  -> index.html  (updated)"
    assert (ep1.root / "page.html").exists()
    # 清单改了之后在项目目录下重跑，各集的数也跟着变
    put(ep1, ".wrally/keyframes/02/pick-k2.png")
    edit(ep1, "02", lambda e: e.update(keyframe={"asset_id": "k2"}))
    CliRunner().invoke(app, ["page", "-C", str(root)])
    assert index_data(root)["episodes"][0]["stats"]["keyframe"] == [1, 4]
