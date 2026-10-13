import anyio
import pytest
from conftest import FakeWorkRally

from wrally.commands.lint import lint
from wrally.commands.new import expand_keys, new
from wrally.errors import NetworkError, PreflightError
from wrally.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    """空工作区（覆盖 conftest 里已经 pull 过的那个）。"""
    return Workspace(tmp_path)


@pytest.fixture
def remote():
    return FakeWorkRally([])


def run(ws, remote, spec="00-02,99", **kwargs):
    kwargs.setdefault("project_id", "proj")
    return anyio.run(lambda: new(ws, remote, spec, **kwargs))


def keys(ws):
    return [s["key"] for s in ws.load()["shots"]]


@pytest.mark.parametrize(("spec", "expected"), [
    ("00-02,99", ["00", "01", "02", "99"]),
    ("3", ["03"]),
    ("12, 13", ["12", "13"]),
])  # fmt: skip
def test_expand_keys(spec, expected):
    assert expand_keys(spec) == expected


@pytest.mark.parametrize("spec", ["", "片头", "05-02", "01,01", "01-", "a-3"])
def test_bad_shots_is_a_preflight_error(spec):
    with pytest.raises(PreflightError) as e:
        expand_keys(spec)
    assert e.value.code == "bad_shots"


def test_new_creates_series_shots_and_manifest(ws, remote):
    report = run(ws, remote, name="EP4 信仰与理性", title="信仰与理性")
    data = ws.load()
    assert (data["project_id"], data["series_id"], data["title"]) == ("proj", "ser1", "信仰与理性")
    assert keys(ws) == ["00", "01", "02", "99"]
    assert [(s["story_num"], s["sort_num"]) for s in remote.stories] == [
        ("片头", 1), ("01", 2), ("02", 3), ("片尾", 4),
    ]  # fmt: skip
    assert all(s["story_id"] for s in data["shots"])
    assert report.summary == "series ser1 (created); added 4 shots, 0 already there  -> episode.yaml"
    assert report.rows[0] == ("00", "片头", "added")


def test_rerun_creates_nothing(ws, remote):
    run(ws, remote, name="EP4")
    before = ws.manifest_path.read_bytes()
    report = run(ws, remote, name="EP4")
    assert remote.count("series_create") == 1 and remote.actions() == ["add"]
    assert ws.manifest_path.read_bytes() == before
    assert (report.series_state, report.added) == ("exists", 0)


def test_adds_only_missing_shots_after_the_last_one(ws, remote):
    run(ws, remote, name="EP4")
    report = run(ws, remote, "00-04,99")
    assert keys(ws) == ["00", "01", "02", "99", "03", "04"]
    assert [(s["story_num"], s["sort_num"]) for s in remote.stories[4:]] == [("03", 5), ("04", 6)]
    assert [r for r in report.rows if r[2] == "added"] == [("03", "03", "added"), ("04", "04", "added")]


def test_dry_run_writes_nothing(ws, remote):
    report = run(ws, remote, name="EP4", dry_run=True)
    assert remote.count("series_create") == 0 and remote.actions() == []
    assert ws.load() is None
    assert report.summary == "series - (would create); would add 4 shots, 0 already there"


def test_same_name_series_is_not_reused_silently(ws, remote):
    remote.series.append({"series_id": "old", "series_name": "EP4"})
    with pytest.raises(PreflightError) as e:
        run(ws, remote, name="EP4")
    assert e.value.code == "series_exists" and "old" in e.value.hint
    assert remote.count("series_create") == 0
    run(ws, remote, series_id="old")
    assert ws.load()["series_id"] == "old" and remote.count("series_create") == 0


def test_series_id_is_found_by_name_when_create_does_not_return_it(ws, remote):
    remote.create_returns_id = False
    run(ws, remote, name="EP4")
    assert ws.load()["series_id"] == "ser1"


def test_failed_add_keeps_series_and_rerun_does_not_duplicate(ws, remote):
    remote.add_outcome = "timeout"  # 服务端已经建了场次，但响应丢了
    with pytest.raises(NetworkError):
        run(ws, remote, name="EP4")
    assert ws.load()["series_id"] == "ser1"  # 剧集 ID 已落盘
    remote.add_outcome = "ok"
    report = run(ws, remote, name="EP4")
    assert remote.count("series_create") == 1 and len(remote.stories) == 4
    assert report.added == 0 and keys(ws) == ["00", "01", "02", "99"]


def test_missing_arguments_and_mismatch(ws, remote):
    for kwargs, code in (({"project_id": None}, "missing_project"), ({}, "missing_name")):
        with pytest.raises(PreflightError) as e:
            run(ws, remote, **kwargs)
        assert e.value.code == code
    run(ws, remote, name="EP4")
    with pytest.raises(PreflightError) as e:
        run(ws, remote, series_id="other")
    assert e.value.code == "series_mismatch"
    assert remote.count("series_create") == 1


def test_placeholder_prompts_fail_lint_until_replaced(ws, remote):
    run(ws, remote, "01", name="EP4")
    assert ws.read_text("prompts/01.image.md") == "（待写）\n"
    assert ws.read_text("prompts/01.anim.md") == "（待写）\n"
    assert [r for r in lint(ws).rows if r[1] == "error"] == [
        ("01", "error", "image: placeholder not replaced"),
        ("01", "error", "anim: placeholder not replaced"),
    ]
    ws.write_text("prompts/01.image.md", "一座钟楼\n")
    ws.write_text("prompts/01.anim.md", "钟轻轻摆动\n")
    assert lint(ws).errors == 0
