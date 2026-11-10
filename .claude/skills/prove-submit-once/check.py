"""穷举检查：video gen 的提交在中断、超时、重跑的任意顺序下是否守住三条性质。

跑的是真实的命令层代码（commands/video.gen、commands/wait.wait、commands/fetch.fetch），远端是
tests/conftest.py 的 FakeWorkRally 加一层按任务记账的模型。从两个起点出发，按广度优先走遍
不超过 --depth 步的所有动作顺序（状态相同的只展开一次），所以报出的反例是最短的。

用法（在仓库根目录）：
    uv run python .claude/skills/prove-submit-once/check.py            # 全部配置，约十几秒
    uv run python .claude/skills/prove-submit-once/check.py --depth 6  # 换深度
    uv run python .claude/skills/prove-submit-once/check.py --trace    # 把每条反例的完整步骤打出来

退出码：0 结果和 KNOWN_FINDINGS 完全一致；1 出现了新的违反；3 已记录的发现不再复现（修好了，
或检查器看不到它了：先弄清是哪一种，再改 KNOWN_FINDINGS 和 SKILL.md）。
"""

import argparse
import copy
import io
import os
import sys
import tempfile
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))

import anyio  # noqa: E402
from conftest import SIGNED, FakeWorkRally, make_story  # noqa: E402
from PIL import Image  # noqa: E402

from wrally.api.models import Shot  # noqa: E402
from wrally.commands import fetch as fetch_cmd  # noqa: E402
from wrally.commands import video as video_cmd  # noqa: E402
from wrally.commands import wait as wait_cmd  # noqa: E402
from wrally.commands.pull import merge  # noqa: E402
from wrally.errors import NetworkError, WrallyError  # noqa: E402
from wrally.workspace import Workspace  # noqa: E402

KEY, STORY = "02", "s02"
ALIVE = ("hidden", "doing")

PROPERTIES = {
    "P1": "输入没变、没加 --redo 时，只有前面受理过的任务全都失败了，服务端才会再受理一个",
    "P2": "清单里标着 done 且 made_from=F 的镜头，服务端确实有一个用 F 做成功的任务",
    "P3": "服务端收到提交请求时，磁盘上的清单已经把这一镜标成 submitting 并记下 made_from",
}
CONFIGS = {
    "visible": "受理的任务立刻计入 doing_count",
    "lag": "受理的任务要过一会儿才计入 doing_count（wait.py 的 pending 就是为这种情况写的）",
}
STARTS = {
    "fresh": "这一镜还没有视频",
    "revised": "这一镜有一条 wrally 做好并下载了的视频，之后改了 video_duration（输入变了）",
}
# 当前代码下已知会违反的（配置, 起点, 性质, 造成违反的那一步, 那一步之前清单里的 status）。
# 说明和反例在 SKILL.md「已知发现」。同一条性质换一种方式被违反（另一步，或另一种状态下）算新的。
KNOWN_FINDINGS: set[tuple[str, str, str, str, str]] = {
    # 任务还没计入 doing_count 时，停在 submitting 的镜头重跑 video gen 会再提交一次
    ("lag", "fresh", "P1", "gen_ok", "submitting"),
    ("lag", "fresh", "P1", "gen_timeout", "submitting"),
    ("lag", "revised", "P1", "gen_ok", "submitting"),
    ("lag", "revised", "P1", "gen_timeout", "submitting"),
}


def shot(ws: Workspace) -> dict:
    return next(e for e in ws.load()["shots"] if e["key"] == KEY)


class Remote(FakeWorkRally):
    """按任务记账的远端：每个受理的任务记下它的输入指纹和状态，结果计数由任务推出来。"""

    def __init__(self, stories: list[dict], ws: Workspace, lag: bool) -> None:
        super().__init__(stories)
        self.ws = ws
        self.lag = lag
        self.tasks: list[dict] = []  # {"fp": 输入指纹, "state": hidden / doing / done / failed}
        self.broken: list[tuple[str, str]] = []  # 受理时发现的违反：(性质, 说明)

    def _generate(self, arguments, retry, kind, per_shot):
        assert retry is False, "提交生成任务不允许自动重试"
        assert kind == "video" and arguments["story_ids"] == [STORY]
        if self.submit_outcome == "lost":
            raise NetworkError(hint="generate TimeoutError")
        video = shot(self.ws).get("video") or {}  # 此刻磁盘上的清单
        fp = video.get("made_from")
        if video.get("status") != "submitting" or not fp:
            self.broken.append(("P3", f"受理时清单里这一镜是 status={video.get('status')} made_from={fp}"))
        twins = [t["state"] for t in self.tasks if t["fp"] == fp and t["state"] in (*ALIVE, "done")]
        if twins:
            said = "还在跑" if twins[0] in ALIVE else "已经成功"
            self.broken.append(("P1", f"同样输入的任务{said}，服务端又受理了一个"))
        self.tasks.append({"fp": fp or "?", "state": "hidden" if self.lag else "doing"})
        self.sync()
        if self.submit_outcome == "timeout":
            raise NetworkError(hint="generate TimeoutError")
        return {"task_ids": [f"task-{len(self.tasks)}"], "skipped": []}

    def sync(self) -> None:
        done = [i for i, t in enumerate(self.tasks) if t["state"] == "done"]
        for i in done:
            self.add_asset(f"v{i}", "video", f"video {i}".encode(), "shot.mp4")
        self.results[(STORY, "video")] = {
            "doing": sum(t["state"] == "doing" for t in self.tasks),
            "failed": sum(t["state"] == "failed" for t in self.tasks),
            "results": [
                {"asset_id": f"v{i}", "asset_url": SIGNED, "created_at": f"2026-01-01 00:{i:02d}:00",
                 "asset_title": "shot.mp4"}
                for i in done
            ],
        }  # fmt: skip

    def move(self, old: str, new: str) -> bool:
        for task in self.tasks:
            if task["state"] == old:
                task["state"] = new
                self.sync()
                return True
        return False


# —— 动作：返回这一步在终端上看到的结果；这一步现在做不了就返回 None ——


async def _gen(ws: Workspace, remote: Remote, outcome: str) -> str:
    remote.submit_outcome = outcome
    before = len(remote.tasks)
    try:
        async with remote.http() as http:
            report = await video_cmd.gen(ws, remote, KEY, http=http)
        said = "; ".join(" ".join(c for c in row[1:] if c) for row in report.rows) or "nothing to do"
    except WrallyError as e:
        said = f"error: {e.code}"
    return f"{said}（服务端{'受理了' if len(remote.tasks) > before else '没有受理'}）"


async def gen_ok(ws, remote):
    return await _gen(ws, remote, "ok")


async def gen_timeout(ws, remote):
    return await _gen(ws, remote, "timeout")


async def gen_lost(ws, remote):
    return await _gen(ws, remote, "lost")


async def _nosleep(_seconds: float) -> None:
    return None


async def wait(ws, remote):
    report = await wait_cmd.wait(ws, remote, "video", KEY, timeout=0, sleep=_nosleep)
    return "; ".join(row[1] for row in report.rows)


async def fetch(ws, remote):
    async with remote.http() as http:
        report = await fetch_cmd.fetch(ws, remote, "video", KEY, http=http)
    return "; ".join(row[1] for row in report.rows)


async def server_done(ws, remote):
    return "" if remote.move("doing", "done") else None


async def server_fail(ws, remote):
    return "" if remote.move("doing", "failed") else None


async def surface(ws, remote):
    return "" if remote.move("hidden", "doing") else None


ACTIONS = {
    "gen_ok": (gen_ok, "跑 wrally video gen，提交请求正常返回"),
    "gen_timeout": (gen_timeout, "跑 wrally video gen，请求到了服务端但回复丢了（超时，或进程在这时被杀）"),
    "gen_lost": (gen_lost, "跑 wrally video gen，请求没到服务端（断网，或进程在落盘 submitting 之后被杀）"),
    "wait": (wait, "跑 wrally wait video（查一次就返回）"),
    "fetch": (fetch, "跑 wrally fetch video"),
    "server_done": (server_done, "服务端：一个在跑的任务成功"),
    "server_fail": (server_fail, "服务端：一个在跑的任务失败"),
    "surface": (surface, "服务端：刚受理的任务开始计入 doing_count"),
}


# —— 状态的保存、还原、判重 ——


def _files(ws: Workspace) -> dict[str, bytes]:
    out = {}
    for path in ws.root.rglob("*"):
        rel = path.relative_to(ws.root).as_posix()
        if path.is_file() and not rel.startswith(".wrally/raw/"):
            out[rel] = path.read_bytes()
    return out


def save(ws: Workspace, remote: Remote):
    return _files(ws), copy.deepcopy(remote.tasks), copy.deepcopy(remote.stories)


def restore(ws: Workspace, remote: Remote, state) -> None:
    files, tasks, stories = state
    for rel in _files(ws).keys() - files.keys():
        (ws.root / rel).unlink()
    for rel, content in files.items():
        path = ws.root / rel
        if not path.is_file() or path.read_bytes() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    remote.tasks = copy.deepcopy(tasks)
    remote.stories[:] = copy.deepcopy(stories)
    remote.broken.clear()
    remote.calls.clear()
    remote.sync()


def key_of(ws: Workspace, remote: Remote) -> tuple:
    entry = shot(ws)
    video = entry.get("video") or {}
    on_disk = bool(video.get("file")) and (ws.root / str(video["file"])).is_file()
    return (
        video.get("status"), video.get("made_from"), video.get("results_before"), video.get("failed_before"),
        video.get("asset_id"), on_disk, tuple((t["fp"], t["state"]) for t in remote.tasks),
    )  # fmt: skip


def violations(ws: Workspace, remote: Remote) -> list[tuple[str, str]]:
    found = list(remote.broken)
    video = shot(ws).get("video") or {}
    fp = video.get("made_from")
    if (
        video.get("status") == "done"
        and fp
        and not any(t["fp"] == fp and t["state"] == "done" for t in remote.tasks)
    ):
        has = [f"{t['state']}" for t in remote.tasks if t["fp"] == fp] or ["没有"]
        found.append(("P2", f"清单标着 done、made_from={fp}，服务端用这份输入的任务：{'、'.join(has)}"))
    return found


# —— 起点 ——


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (96, 54), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


async def build(root: Path, lag: bool, start: str) -> tuple[Workspace, Remote, list[tuple[str, str]]]:
    """和 tests/test_video_gen.py 的 ready 夹具一样：02 镜已选用 key02，提示词已 push，有视频默认值。"""
    stories = [
        make_story("01", "s01", 1),
        make_story("02", STORY, 2, keyframe="key02"),
    ]
    ws = Workspace(root)
    merge(ws, [Shot.from_story(s) for s in stories], project_id="proj", series_id="ser")
    data = ws.load()
    data["defaults"] = {"video": {"model": "vid1", "resolution": 1, "duration": 8, "audio": False}}
    ws.save(data)
    remote = Remote(stories, ws, lag)
    remote.add_asset("key02", "image", _png(), "02.png")
    remote.sync()
    if start == "revised":
        # 用真实命令做出「已有一条做好的视频，之后改了输入」
        await gen_ok(ws, remote)
        remote.move("hidden", "doing")
        remote.move("doing", "done")
        await fetch(ws, remote)
        data = ws.load()
        next(e for e in data["shots"] if e["key"] == KEY)["video_duration"] = 4
        ws.save(data)
    bad = violations(ws, remote)  # 搭起点用的也是真实命令：它们自己违反了性质也要报
    if start == "revised" and not bad and shot(ws)["video"].get("status") != "done":
        raise RuntimeError("the `revised` start state could not be built: gen + fetch did not end in done")
    remote.broken.clear()
    return ws, remote, bad


# —— 穷举 ——


Found = dict[tuple[str, str, str], tuple[str, list]]


async def explore(lag: bool, start: str, depth: int) -> tuple[int, Found]:
    """返回（走到的状态数，(性质, 造成违反的那一步, 之前的 status) → (说明, 最短反例的步骤)）。"""
    with tempfile.TemporaryDirectory(prefix="wrally-prove-") as tmp:
        os.environ["WRALLY_CONFIG_DIR"] = str(Path(tmp) / "cfg")
        ws, remote, bad = await build(Path(tmp) / "ep", lag, start)
        if bad:
            return 0, {(prop, "start", "-"): (why, []) for prop, why in bad}
        names = [n for n in ACTIONS if lag or n != "surface"]
        first = save(ws, remote)
        seen = {key_of(ws, remote)}
        queue = deque([(first, [])])
        found: Found = {}
        while queue:
            state, path = queue.popleft()
            if len(path) >= depth:
                continue
            for name in names:
                restore(ws, remote, state)
                was = str((shot(ws).get("video") or {}).get("status") or "-")
                said = await ACTIONS[name][0](ws, remote)
                if said is None:
                    continue
                step = path + [(name, said)]
                bad = violations(ws, remote)
                for prop, why in bad:
                    found.setdefault((prop, name, was), (why, step))
                if bad:
                    continue  # 违反之后的状态不再往下走
                key = key_of(ws, remote)
                if key not in seen:
                    seen.add(key)
                    queue.append((save(ws, remote), step))
        return len(seen), found


def tell(start: str, why: str, steps: list) -> str:
    lines = [f"    起点：{STARTS[start]}"]
    if not steps:
        lines.append(
            "    （搭这个起点的那几步真实命令就已经违反了：video gen 提交 → 任务成功 → fetch video）"
        )
    for n, (name, said) in enumerate(steps, 1):
        lines.append(f"    {n}. {ACTIONS[name][1]}" + (f" → {said}" if said else ""))
    lines.append(f"    结果：{why}")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--depth", type=int, default=7, help="最多走几步（默认 7）")
    parser.add_argument("--trace", action="store_true", help="打出每条反例的完整步骤")
    args = parser.parse_args()
    violated: dict[tuple[str, str, str, str, str], tuple[str, list]] = {}
    for config in CONFIGS:
        for start in STARTS:
            states, found = await explore(config == "lag", start, args.depth)
            marks = []
            for prop in PROPERTIES:
                hits = {(config, start, *sig): v for sig, v in found.items() if sig[0] == prop}
                violated |= hits
                fresh = len(hits.keys() - KNOWN_FINDINGS)
                if fresh:
                    marks.append(f"{prop} VIOLATED")
                else:
                    marks.append(f"{prop} known finding" if hits else f"{prop} holds")
            print(f"{config:8} {start:8} depth {args.depth}  {states:4} states   {'   '.join(marks)}")
    new, gone = violated.keys() - KNOWN_FINDINGS, KNOWN_FINDINGS - violated.keys()
    for sig, (why, steps) in violated.items():
        if args.trace or sig in new:
            config, start, prop = sig[:3]
            print(f"\n{sig}\n{prop}（{PROPERTIES[prop]}）")
            print(f"在「{CONFIGS[config]}」下不成立，最短 {len(steps)} 步：")
            print(tell(start, why, steps))
    if new:
        print(f"\nFAIL: {len(new)} new violation(s): {sorted(new)}")
        return 1
    if gone:
        print(f"\nCHANGED: known finding(s) no longer reproduce: {sorted(gone)}")
        return 3
    print(f"\nOK: matches the {len(KNOWN_FINDINGS)} known finding(s); everything else holds within the bound")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
