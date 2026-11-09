import json

import anyio
import pytest
from typer.testing import CliRunner

from wrally import cli
from wrally.api import projects as projects_api
from wrally.commands import projects as projects_cmd
from wrally.errors import NetworkError, PreflightError
from wrally.workspace import Workspace


@pytest.fixture
def listed(remote):
    remote.projects = [
        {"project_id": "p1", "project_name": "提灯人"},
        {"project_id": "p2", "project_name": "三体童话"},
        {"project_id": "p3", "project_name": "旧项目", "trash": True},
    ]
    remote.series_by_project = {"p1": 7, "p2": 3, "p3": 0}
    return remote


async def test_pages_through_all_projects_and_drops_urls(listed, monkeypatch):
    monkeypatch.setattr(projects_api, "PAGE_SIZE", 1)
    found = await projects_api.project_list(listed)
    assert [p.project_id for p in found] == ["p1", "p2"]  # 不含回收站
    assert found[0].created == "2026-10-04" and "cdn.example.com" not in found[0].model_dump_json()
    assert listed.count("project_list") == 2
    assert all("filter" not in args for t, args in listed.calls if t == "project_list")  # 1 会带上回收站的


async def test_search_and_trash(listed):
    assert [p.name for p in await projects_api.project_list(listed, search="三体")] == ["三体童话"]
    assert [p.name for p in await projects_api.project_list(listed, trash=True)] == ["旧项目"]


def test_cli_counts_episodes_and_marks_the_current_project(ws, listed, monkeypatch):
    data = ws.load()
    data["project_id"] = "p1"
    ws.save(data)
    monkeypatch.setattr(cli, "_remote", lambda verbose, work: anyio.run(work, listed))
    result = CliRunner().invoke(cli.app, ["projects", "-C", str(ws.root), "--json"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["summary"] == "2 projects"
    assert [(r["id"], r["episodes"], r["here"]) for r in out["rows"]] == [("p1", "7", "*"), ("p2", "3", "")]


async def test_create_writes_the_project_file_and_skips_the_default_series(tmp_path, listed):
    report = await projects_cmd.create(listed, tmp_path, "新片：第一部", canvas="1080:1920")
    assert report.project_id == "np4" and report.file_state == "written"
    args = next(a for t, a in listed.calls if t == "project_create")
    assert args["auto_create_series"] is False and args["canvas_size_value"] == "1080:1920"
    episode = tmp_path / "ep1"
    episode.mkdir()
    assert Workspace(episode).project()["project_id"] == "np4"  # 子目录里的 new 能读到
    assert Workspace(episode).project()["title"] == "新片：第一部"  # 名字里有冒号也是合法的 YAML
    assert projects_cmd.local_project_id(tmp_path) == "np4"  # 项目目录自己也认得


async def test_dry_run_and_same_name_stop_before_creating(tmp_path, listed):
    report = await projects_cmd.create(listed, tmp_path, "新片", dry_run=True)
    assert report.summary == "dry run: would create project '新片'; wrally.yaml (would write)"
    with pytest.raises(PreflightError) as err:
        await projects_cmd.create(listed, tmp_path, "提灯人")
    assert err.value.code == "project_exists" and "p1" in err.value.hint
    assert listed.count("project_create") == 0 and not (tmp_path / "wrally.yaml").exists()


@pytest.mark.parametrize(
    "setup, code",
    [
        (lambda d: (d / "wrally.yaml").write_text("project_id: p9\n"), "project_file_exists"),
        (lambda d: (d / "episode.yaml").write_text("shots: []\n"), "episode_directory"),
    ],
)
async def test_wrong_directory_stops_before_creating(tmp_path, listed, setup, code):
    setup(tmp_path)
    with pytest.raises(PreflightError) as err:
        await projects_cmd.create(listed, tmp_path, "新片")
    assert err.value.code == code and listed.count("project_create") == 0


async def test_existing_project_file_without_id_is_not_touched(tmp_path, listed):
    (tmp_path / "wrally.yaml").write_text("title: 手写的\n", encoding="utf-8")
    report = await projects_cmd.create(listed, tmp_path, "新片")
    assert report.file_state == "kept" and "project_id: np4" in report.notes[0]
    assert (tmp_path / "wrally.yaml").read_text(encoding="utf-8") == "title: 手写的\n"


@pytest.mark.parametrize("outcome", ["timeout", "no_id", "bad_response"])
async def test_lost_response_is_recovered_by_name(tmp_path, listed, outcome):
    listed.project_outcome = outcome
    report = await projects_cmd.create(listed, tmp_path, "新片")
    assert report.project_id == "np4" and listed.count("project_create") == 1


async def test_request_that_never_arrived_raises_and_writes_nothing(tmp_path, listed):
    listed.project_outcome = "lost"
    with pytest.raises(NetworkError):
        await projects_cmd.create(listed, tmp_path, "新片")
    assert not (tmp_path / "wrally.yaml").exists()
