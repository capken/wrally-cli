---
name: prove-submit-once
description: 穷举检查 wrally video gen 的提交、中断恢复和 wait / fetch 的状态回写：任意顺序的超时、断网、进程被杀、重跑之下，会不会重复提交（多花额度），会不会把旧视频记成用新输入做好的。改了 commands/video.py、commands/wait.py、commands/fetch.py、api/results.py 里和 status、made_from、results_before、failed_before、submitting 有关的逻辑之后，或者要把同样的幂等改动同步到 keyframe gen、audio gen 之前，先跑它。
---

# 穷举检查：视频提交不重复、done 不说谎

单元测试各写了一种中断的情形。这个检查把真实的命令层代码放在一个假远端上，走遍「提交的三种结局 × 服务端的进展 × 用户接着敲哪条命令」在给定步数内的所有顺序，每一步之后核对三条性质。它跑的是 `src/wrally` 里的代码本身，不是另写的模型，所以不存在模型和代码对不上的问题；要对得上的是假远端和真远端。

## 三条性质

| | 性质 | 守不住的后果 |
| --- | --- | --- |
| P1 | 输入没变、没加 `--redo` 时，只有前面受理过的任务全都失败了，服务端才会再受理一个 | 同一镜同样的输入生成两次，多花一次真实额度 |
| P2 | 清单里标着 `done` 且 `made_from` 是 F 的镜头，服务端确实有一个用 F 做成功的任务 | 清单说视频是用现在的输入做的，文件其实是旧的；`video gen` 跳过它，`check` 不报过期，`export` 把旧视频带进成片 |
| P3 | 服务端收到提交请求时，磁盘上的清单已经把这一镜标成 `submitting` 并记下 `made_from` | 进程在提交后被杀，清单上没有痕迹，重跑时无从判断任务是否已受理 |

## 什么时候要跑

改动碰到下面任何一处就跑。右边一列是检查实际执行到的代码。

| 文件 | 函数或片段 |
| --- | --- |
| `src/wrally/commands/video.py` | `gen` 里的分类（`todo` / `recover`）、恢复时的 `rs.doing > 0 or rs.done > results_before`、落盘 `submitting`、提交后写 `running`；`fingerprint`、`shot_settings` |
| `src/wrally/commands/wait.py` | `wait` 里的 `pending`、`baseline`、最后给每镜定 `label` 并写回 `status` 的那一段（含 `unconfirmed` 分支） |
| `src/wrally/commands/fetch.py` | `awaited`（拿提交时的基线判断这次任务留下了什么）、`fetch`、`_media`（写 `status`、`asset_id`、`file`） |
| `src/wrally/api/results.py` | `ResultSet.state`、`get_results` |
| `src/wrally/api/video.py` | `submit_video`（`retry=False`）、`configure` |
| `tests/conftest.py` | `FakeWorkRally`：检查器的假远端继承它，`make_story` 也取自这里 |

**核对到的提交：`d279aee`**（2026-11-10）。上表的文件在这之后有改动（`git log d279aee.. -- <上表的文件>`），就说明检查描述的可能已经不是现在的代码：先按下面的步骤重跑并读结果，确认它仍然覆盖这些代码，再把这里的提交号往前挪。没确认就不要挪。

## 怎么跑

在仓库根目录：

```
uv run python .claude/skills/prove-submit-once/check.py            # 约 6 秒
uv run python .claude/skills/prove-submit-once/check.py --trace    # 连已知发现的反例也打出来
uv run python .claude/skills/prove-submit-once/check.py --depth 20 # 约 20 秒，合并前跑一次
```

没有额外依赖：用的是开发环境里已有的包和 `tests/conftest.py`。它在临时目录里建工作区，不碰本机凭证，不调远端。

输出每行是一种「远端配置 × 起点」，后面是三条性质各自的结论：

```
visible  fresh    depth 7    35 states   P1 holds   P2 holds   P3 holds
visible  revised  depth 7    34 states   P1 holds   P2 holds   P3 holds
lag      fresh    depth 7    29 states   P1 known finding   P2 holds   P3 holds
lag      revised  depth 7    28 states   P1 known finding   P2 holds   P3 holds

OK: matches the 4 known finding(s); everything else holds within the bound
```

| 退出码 | 含义 | 该做什么 |
| --- | --- | --- |
| 0 | 和已登记的发现完全一致，其余都成立 | 你的改动没有让事情变坏 |
| 1 | `VIOLATED`：出现了新的违反，下面打出最短的反例 | 读反例，按下一节判断 |
| 3 | `CHANGED`：登记过的某条发现不再复现 | 多半是修好了：见「改了行为之后」 |

反例是一串编号的步骤，每步是用户敲的一条命令或服务端的一个变化，箭头后面是那条命令在终端上显示的结果。

## 是真问题还是检查器的问题

1. 照反例的步骤读代码，每一步的结果都要能在上表的函数里找到出处。反例只有几步，都能手工对出来。
2. 步骤里的服务端行为（见下面「假远端的假设」）真远端做不出来，那是检查器的问题：改 `check.py` 里的 `Remote`，在这里写明依据。
3. 步骤都站得住，就是真问题。报告给用户时用反例原样的步骤。不要为了让它通过去放宽性质、删动作、把它加进 `KNOWN_FINDINGS`；也不要顺手改产品代码，除非用户要求修。
4. `RuntimeError: the revised start state could not be built`：搭起点的几条真实命令（`video gen` → 任务成功 → `fetch video`）没有走到 `done`，是比违反性质更基本的坏法，同样当真问题查。

## 已知发现

当前代码下性质不成立的情形登记在 `check.py` 的 `KNOWN_FINDINGS` 里，现在是 4 条签名（配置、起点、性质、造成违反的那一步、那一步之前清单里的 `status`），是同一件事。产品代码没有为它改动过。

**受理的任务还没计入 `doing_count` 时，重跑 `video gen` 会再提交一次（P1，只在 `lag` 配置下）。**

`wrally video gen` 的请求到了服务端、回复丢了，清单停在 `submitting`。马上重跑：恢复逻辑看到 `doing` 是 0、结果数没变，判定「没受理」，再提交一次。出处：`video.py` 恢复时的 `rs.doing > 0 or rs.done > results_before`。

`lag` 配置的依据是代码自己的说法：`wait.py` 的 `pending` 注明「刚提交的任务可能还没出现在 doing 里」，`tests/test_video_gen.py` 有对应的测试。这段延迟在真远端上有多长没有量过；它越短，这条发现越难碰上。单看远端的计数分不清「没受理」和「受理了还没显示」，要修得等一等再查，或者换一种能确认受理的办法。

## 修掉的发现

**`wait` 和 `fetch` 把以前的结果当成这次任务的结果（P2）。** 2026-11-10 发现并修复，当时登记的 10 条签名已删除。

一镜已有做好的视频、之后改了输入：提交没到服务端（清单停在 `submitting`）后跑 `wait video` 或 `fetch video`，或者提交成功、任务失败后不经过 `wait` 直接 `fetch video`，这一镜都会被写成 `done`，清单记着新输入的指纹配旧视频。修法是 `fetch.awaited`：清单在等任务（`submitting` 或 `running`）时，只拿提交时记的 `results_before`、`failed_before` 判断这次任务留下了什么。远端没有痕迹的 `submitting` 镜头不改清单，表格里写 `unconfirmed (run: wrally video gen)`。这也顺带消掉了 `lag` 配置下「提交后马上 `fetch`，状态被改成 `pending` 或 `failed`，再跑 `video gen` 重复提交」的 4 条签名。

单元测试：`tests/test_fetch.py::test_fetch_does_not_take_an_older_result_for_the_task_it_is_waiting_for`、`tests/test_wait_status.py::test_wait_leaves_an_interrupted_submit_to_the_gen_command`。

## 范围

查的是：一镜（`02`）、`video gen` 不带 `--redo`、输入在探索过程中不变；两个起点（没有视频；已有一条做好的视频而后改了 `video_duration`）；两种远端（受理的任务立刻或延后计入 `doing_count`）；动作是 `video gen` 的三种结局（正常返回；服务端受理了但回复丢失；请求没到服务端）、`wait video`（查一次就返回）、`fetch video`、服务端让一个在跑的任务成功或失败；默认最多 7 步。进程在任意一行被杀等价于后两种结局之一，因为 `gen` 在落盘 `submitting` 和提交之间没有别的写入。

状态空间没有上限（失败的任务可以一直累积），所以结论只对步数以内成立。深度 20 跑过一次，没有出现新的签名。

没查的：

- `keyframe gen`、`audio gen`、`gen`、`video upscale`。它们的骨架是各写一份的，这里的结论不能搬过去。
- 多镜一起提交、服务端跳过个别镜头（`skipped_by_server`）、`--redo`、`--dry-run`、首尾帧。
- 探索途中改输入；`status` 是 `submitting` 时改了输入再跑 `video gen`（代码会不问远端直接提交）。
- 有人同时在网页上生成；两个 wrally 进程同时跑。
- 真远端。假远端的假设见下。

假远端的假设（`check.py` 的 `Remote`）：受理即建任务，任务只会从在跑变成成功或失败；`doing_count`、`failed_count` 和结果列表如实反映任务（`lag` 配置下新任务先不计入）；回复丢失时任务照样存在；`shotlist_get_result` 和其他只读调用不会失败。

## 改了行为之后

- **修掉了一条发现。** 检查以 3 退出并列出不再复现的签名。先用 `--trace` 确认它是真的不成立了，而不是检查器走不到那里了（比如起点建不出来、状态数骤降）。然后从 `KNOWN_FINDINGS` 删掉这些签名，把上面「已知发现」里对应的段落删掉或改写，重跑到退出码 0。
- **有意改了行为，性质本身要变。** 改 `PROPERTIES` 的文字和 `Remote._generate`、`violations` 里对应的判断，并在这份文件的性质表里同步。改完做一次下面的「会不会报错」。
- **加了命令或状态。** 新的用户动作加进 `ACTIONS`；影响后续行为的新清单字段加进 `key_of`，否则两个不同的状态会被当成一个，漏掉后面的路径。
- 每次改完 `check.py`，`uv run ruff check` 和 `uv run ruff format` 过一遍。

## 会不会报错

一个从没见过失败的检查什么都证明不了。生成这个 skill 时（`63bf9d7`）在真实代码上各做了一处临时改动，检查都以退出码 1 报出新的违反，改回后回到退出码 0、`uv run pytest -q` 全部通过（P2 那一处在修复之后又做了一遍，照样报出）：

| 性质 | 临时改动 | 报出的 |
| --- | --- | --- |
| P1 | `video.py` 恢复条件去掉 `rs.doing > 0 or` | `visible` 配置下 `video gen` 超时后重跑又提交一次（2 步） |
| P2 | `fetch.py` 的 `if sec.get("status") == "failed"` 改成 `if False` | 任务失败 → `wait` 标 `failed` → `fetch` 记成 `done`（4 步） |
| P3 | `video.py` 去掉提交前的 `ws.save(data)` | 服务端受理时清单里 `status=None`（1 步） |

改了 `check.py` 的判断逻辑之后重做一遍这三处，确认它还报得出来。临时改动用完即还原，`git status` 里 `src/` 不应该有改动。

## 维护

代码改了而没有重跑这个检查时，用 `/pstack:maintain-formal-verification-skill` 把它对齐。
