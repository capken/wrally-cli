"""本地文件上传成媒资：wrally upload、gen 的本地路径、清单里写成文件的 ref_assets 和音色。"""

import json

import pytest
from conftest import entry

from wrally import localfile
from wrally.api import upload as upload_api
from wrally.commands import gen
from wrally.commands import upload as upload_cmd
from wrally.commands.audio import gen as audio_gen
from wrally.commands.check import stale
from wrally.commands.keyframe_gen import gen as keyframe_gen
from wrally.commands.lint import lint
from wrally.errors import PreflightError

pytestmark = pytest.mark.anyio


def edit(ws, key=None, **fields):
    data = ws.load()
    target = data if key is None else next(e for e in data["shots"] if e["key"] == key)
    target.update(fields)
    ws.save(data)


def cache():
    return upload_api.load_cache()


@pytest.mark.parametrize(
    ("value", "local"),
    [("ymukwr4k1d", False), ("sheetA", False), ("a.png", True), ("refs/a", True),
     ("~/x.wav", True), ("", False), (None, False)],
)  # fmt: skip
def test_paths_and_asset_ids_are_told_apart(value, local):
    assert localfile.is_local(value) is local


def test_unsupported_and_missing_files_fail_before_any_call(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(PreflightError) as e:
        localfile.check_file(tmp_path / "a.txt")
    assert e.value.code == "unsupported_file"
    with pytest.raises(PreflightError) as e:
        localfile.check_file(tmp_path / "gone.png")
    assert e.value.code == "missing_file"


async def test_upload_puts_to_cos_then_creates_asset_and_remembers_it(tmp_path, remote):
    img = tmp_path / "公主.png"
    img.write_bytes(b"PNG1")
    [got] = await upload_api.upload(remote, [img], "proj", http=remote.http())
    assert (got.asset_id, got.state) == ("up1", "uploaded")
    [put] = remote.put_headers
    # 只签 host，带临时凭证的 token；不带 Content-Type（凭证的策略不允许）
    assert "q-ak=AKIDtest" in put["authorization"] and "q-header-list=host" in put["authorization"]
    assert put["x-cos-security-token"] == "tok"
    [created] = remote.created
    assert created["project_id"] == "proj" and created["asset_type"] == "image"
    assert created["name"] == "公主.png" and created["sha1"] == localfile.sha1(img)
    assert created["asset_url"] == "https://cdn.example.com/up/0.png"  # 用凭证给的 file_url，不自己拼
    assert [v["asset_id"] for v in cache().values()] == ["up1"]
    assert "secret" not in json.dumps(cache()) and "cdn.example.com" not in json.dumps(cache())

    # 同一个文件再传：媒资还在就不再上传
    [again] = await upload_api.upload(remote, [img], "proj", http=remote.http())
    assert (again.asset_id, again.state) == ("up1", "cached")
    assert remote.count("get_upload_token") == 1 and len(remote.created) == 1


async def test_same_file_twice_in_one_call_is_uploaded_once(tmp_path, remote):
    img = tmp_path / "a.png"
    img.write_bytes(b"PNG1")
    copy = tmp_path / "b.png"
    copy.write_bytes(b"PNG1")  # 内容相同，换了名字
    got = await upload_api.upload(remote, [img, copy], "proj", http=remote.http())
    assert [(g.asset_id, g.state) for g in got] == [("up1", "uploaded"), ("up1", "cached")]
    assert len(remote.put_headers) == 1


async def test_deleted_asset_is_uploaded_again(tmp_path, remote):
    img = tmp_path / "a.png"
    img.write_bytes(b"PNG1")
    await upload_api.upload(remote, [img], "proj", http=remote.http())
    del remote.assets["up1"]  # 媒资被删了：asset_detail 不再返回它
    [got] = await upload_api.upload(remote, [img], "proj", http=remote.http())
    assert (got.asset_id, got.state) == ("up2", "uploaded")


async def test_another_project_gets_its_own_upload(tmp_path, remote):
    img = tmp_path / "a.png"
    img.write_bytes(b"PNG1")
    await upload_api.upload(remote, [img], "proj", http=remote.http())
    [got] = await upload_api.upload(remote, [img], "other", http=remote.http())
    assert got.state == "uploaded" and remote.created[-1]["project_id"] == "other"


async def test_upload_command_without_project_uses_the_default_project(tmp_path, remote, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "v.wav").write_bytes(b"RIFF")
    remote.projects = [
        {"project_id": "dflt", "project_name": "默认项目"},
        {"project_id": "p2", "project_name": "x"},
    ]
    line, rows = await upload_cmd.run(
        remote, ["v.wav"], project_id=None, directory=tmp_path, http=remote.http()
    )
    assert rows == [("v.wav", "up1", "uploaded")] and "project dflt" in line
    assert remote.created[0]["asset_type"] == "audio" and remote.created[0]["project_id"] == "dflt"


async def test_upload_command_dry_run_does_not_upload(tmp_path, remote, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.png").write_bytes(b"PNG1")
    line, rows = await upload_cmd.run(remote, ["a.png"], project_id="proj", directory=tmp_path, dry_run=True)
    assert rows == [("a.png", "-", "would upload")] and line.startswith("dry run: would upload 1 of 1")
    assert remote.calls == []


async def test_upload_command_takes_the_project_of_the_episode(ws, remote, monkeypatch):
    monkeypatch.chdir(ws.root)
    (ws.root / "a.png").write_bytes(b"PNG1")
    await upload_cmd.run(remote, ["a.png"], project_id=None, directory=ws.root, http=remote.http())
    assert remote.created[0]["project_id"] == "proj" and remote.count("project_list") == 0


# ---------- gen ----------


async def test_gen_with_a_local_reference_uploads_it_first(tmp_path, remote, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sheet.png").write_bytes(b"PNG1")
    params = {"prompt": "第一张图片里的人", "model": "img9", "count": 1, "refs": ["sheet.png"], "name": "x"}
    report = await gen.gen(remote, "image", params, tmp_path / "out", project_id="proj", http=remote.http())
    assert report.summary.endswith("; uploaded 1 local file(s)")
    assert remote.canvas_requests[0]["input_images"] == ["https://dl.example.com/up1"]
    [job] = gen.load_log(tmp_path / "out")
    assert job["params"]["refs"] == ["up1"]  # 记录里存媒资 ID，不存本地路径

    # 同样的参数再跑：文件不再上传，按媒资 ID 认出是同一批
    again = await gen.gen(remote, "image", params, tmp_path / "out", project_id="proj", http=remote.http())
    assert "already submitted as job 001" in again.summary and len(remote.created) == 1


async def test_gen_dry_run_does_not_upload(tmp_path, remote, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "first.png").write_bytes(b"PNG1")
    params = {"prompt": "p", "model": "vid9", "count": 1, "mode": "firstlast", "image": "first.png",
              "last": None, "duration": 4}  # fmt: skip
    report = await gen.gen(remote, "video", params, tmp_path, dry_run=True)
    assert report.summary.endswith("; would upload 1 local file(s)")
    assert remote.count("get_upload_token") == 0 and remote.put_headers == []


async def test_gen_with_a_missing_local_file_stops_before_any_upload(tmp_path, remote, monkeypatch):
    monkeypatch.chdir(tmp_path)
    params = {"prompt": "p", "model": "aud9", "count": 1, "voices": ["voice.wav"]}
    with pytest.raises(PreflightError) as e:
        await gen.gen(remote, "audio", params, tmp_path)
    assert e.value.code == "missing_file" and remote.count("get_upload_token") == 0


# ---------- 清单 ----------


@pytest.fixture
def project_ref(tmp_path, stories, remote):
    """项目目录里登记一张本地的角色设定图（相对项目目录），99 镜引用它。"""
    from wrally.api.models import Shot
    from wrally.commands.pull import merge
    from wrally.workspace import Workspace

    project = tmp_path / "film"
    (project / "art").mkdir(parents=True)
    (project / "art" / "公主.png").write_bytes(b"PNG-princess")
    (project / "wrally.yaml").write_text("ref_assets:\n  公主: art/公主.png\n", encoding="utf-8")
    ws = Workspace(project / "ep1")
    merge(ws, [Shot.from_story(s) for s in stories], project_id="proj", series_id="ser")
    edit(ws, defaults={"image": {"model": "img9", "resolution": 4, "aspect": "16:9", "count": 2}})
    edit(ws, "99", keyframe={"refs": ["公主"]})
    return ws, remote


async def test_keyframe_refs_can_be_local_files_in_the_project_file(project_ref):
    ws, remote = project_ref
    dry = await keyframe_gen(ws, remote, "99", dry_run=True)
    assert dry.rows == [("99", "would submit", "")] and remote.calls == []

    report = await keyframe_gen(ws, remote, "99", http=remote.http())
    assert report.rows == [("99", "submitted", "2 images via canvas, refs 公主")]
    assert remote.created[0]["name"] == "公主.png" and remote.created[0]["project_id"] == "proj"
    assert remote.canvas_requests[0]["input_images"] == ["https://dl.example.com/up1"]
    again = await keyframe_gen(ws, remote, "99", dry_run=True)
    assert again.rows[0][2].startswith("running, inputs unchanged")

    # 指纹按文件内容：换了内容才算输入变了（这时上一批还在跑，所以先等它）
    (ws.root.parent / "art" / "公主.png").write_bytes(b"PNG-princess-v2")
    changed = await keyframe_gen(ws, remote, "99", dry_run=True)
    assert changed.rows == [("99", "skipped", "another task is running; wait for it first")]


async def test_lint_reports_a_missing_local_ref_file(project_ref):
    ws, _ = project_ref
    (ws.root.parent / "art" / "公主.png").unlink()
    assert ("99", "error", "image: ref file missing 公主.png") in lint(ws, "99").rows


async def test_voice_can_be_a_local_file(ws, remote):
    ws.write_text("styles/n.md", "使用<{voice_name}>的音色。他说：「{narration}」\n")
    (ws.root / "voice.wav").write_bytes(b"RIFF-voice")
    edit(ws, defaults={"audio": {"model": "aud1", "voice_asset": "voice.wav", "voice_name": "旁白男声"},
                       "narration_template": "styles/n.md"})  # fmt: skip
    edit(ws, "02", audio_prompt=None, narration="台词02")
    report = await audio_gen(ws, remote, "02", http=remote.http())
    assert report.submitted == ["02"]
    assert remote.created[0]["asset_type"] == "audio"
    story = next(s for s in remote.stories if s["story_id"] == "s02")
    bound = json.loads(json.loads(story["extra"])["audio_role_data_json"])
    assert [b["asset_id"] for b in bound] == ["up1"]  # 绑定的是上传后的媒资
    data = ws.load()
    assert stale(ws, data, entry(ws, "02")) == []  # 指纹按文件内容，check 判断得出来
    (ws.root / "voice.wav").write_bytes(b"RIFF-other")
    assert stale(ws, ws.load(), entry(ws, "02")) == ["audio was made from different narration"]
