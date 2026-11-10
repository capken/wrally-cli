"""shots rm / shots order：删场次、按清单重排场次。"""

import pytest

from wrally.commands.shots import order, rm
from wrally.errors import PreflightError, ToolError

pytestmark = pytest.mark.anyio


def keys(ws):
    return [str(e["key"]) for e in ws.load()["shots"]]


def remote_order(remote):
    return [s["story_num"] for s in sorted(remote.stories, key=lambda s: s["sort_num"])]


# ---------- rm ----------


async def test_rm_deletes_on_remote_and_moves_the_entry_aside(ws, remote):
    report = await rm(ws, remote, "01")
    assert [s["story_id"] for s in remote.stories] == ["s00", "s02", "s99"]
    assert keys(ws) == ["00", "02", "99"]
    folder = ws.state_dir / "removed" / "01-s01"
    assert "story_id: s01" in (folder / "entry.yaml").read_text(encoding="utf-8")  # 条目留着，可以找回
    # 提示词文件挪走了，不留在 prompts/ 里（以后再建 01 镜时不会被当成手写的提示词）
    assert (folder / "01.image.md").is_file() and not (ws.root / "prompts" / "01.image.md").exists()
    assert report.rows[0][:2] == ("01", "s01") and report.rows[0][2].startswith("deleted")
    [(_, args)] = [(t, a) for t, a in remote.calls if t == "shotlist_manage" and a["action"] == "delete"]
    assert args["story_ids"] == ["s01"] and args["series_id"] == "ser"


async def test_rm_dry_run_changes_nothing(ws, remote):
    before = ws.manifest_path.read_text(encoding="utf-8")
    report = await rm(ws, remote, "01,02", dry_run=True)
    assert [r[2] for r in report.rows] == ["would delete", "would delete"]
    assert len(remote.stories) == 4 and ws.manifest_path.read_text(encoding="utf-8") == before
    assert "delete" not in remote.actions()


async def test_rm_refuses_all(ws, remote):
    with pytest.raises(PreflightError) as e:
        await rm(ws, remote, "all")
    assert e.value.code == "bad_shots" and remote.calls == []


async def test_rm_after_an_interrupted_delete_only_fixes_the_manifest(ws, remote):
    """上次远端已删、清单还没改（比如删除的响应丢了）：重跑不再删，只改本地。"""
    remote.stories[:] = [s for s in remote.stories if s["story_id"] != "s01"]
    report = await rm(ws, remote, "01")
    assert "delete" not in remote.actions() and keys(ws) == ["00", "02", "99"]
    assert report.rows[0][2].startswith("dropped locally")


async def test_rm_warns_when_other_shots_use_it_as_a_ref(ws, remote):
    data = ws.load()
    data["shots"][3]["keyframe"] = {"refs": ["02"]}
    ws.save(data)
    report = await rm(ws, remote, "02")
    assert report.notes == ["note: 99 use these shots as refs; edit their keyframe.refs"]


async def test_rm_that_did_not_take_effect_is_an_error(ws, remote):
    async def ignore(tool, arguments, **kw):
        if arguments.get("action") == "delete":
            return {"message": "ok"}
        return await original(tool, arguments, **kw)

    original, remote.call = remote.call, ignore
    with pytest.raises(ToolError) as e:
        await rm(ws, remote, "01")
    assert e.value.code == "shots_not_deleted" and "01" in keys(ws)


# ---------- order ----------


async def test_order_follows_the_manifest(ws, remote):
    data = ws.load()
    data["shots"].insert(1, data["shots"].pop(2))  # 本地把 02 挪到 01 前面
    ws.save(data)
    dry = await order(ws, remote, dry_run=True)
    assert dry.rows == [("02", "3", "2"), ("01", "2", "3")] and remote_order(remote)[1] == "01"
    report = await order(ws, remote)
    assert remote_order(remote) == ["片头", "02", "01", "片尾"]
    assert report.summary == "moved 2 shots on WorkRally to match episode.yaml"
    again = await order(ws, remote)
    assert again.summary == "WorkRally order already matches episode.yaml"
    assert [a for a in remote.actions() if a == "sort"] == ["sort"]  # 第二次没再写远端


async def test_order_by_key_sorts_the_manifest_first(ws, remote):
    data = ws.load()
    data["shots"].append(data["shots"].pop(0))  # 00 跑到了最后
    ws.save(data)
    for s in remote.stories:  # 远端也是这个顺序
        s["sort_num"] = {"s01": 1, "s02": 2, "s99": 3, "s00": 4}[s["story_id"]]
    report = await order(ws, remote, by_key=True)
    assert keys(ws) == ["00", "01", "02", "99"]
    assert report.notes == ["note: --by-key reorders episode.yaml; export and page.html follow that order"]
    assert remote_order(remote) == ["片头", "01", "02", "片尾"]


async def test_order_needs_the_same_shots_on_both_sides(ws, remote):
    remote.stories.append({**remote.stories[1], "story_id": "s05", "story_num": "05", "sort_num": 9})
    with pytest.raises(PreflightError) as e:
        await order(ws, remote)
    assert e.value.code == "not_in_manifest" and "05" in e.value.hint and "sort" not in remote.actions()


async def test_order_that_did_not_take_effect_is_an_error(ws, remote):
    data = ws.load()
    data["shots"].insert(1, data["shots"].pop(2))
    ws.save(data)
    remote.sort_ignored = True
    with pytest.raises(ToolError) as e:
        await order(ws, remote)
    assert e.value.code == "order_not_applied"
