---
name: wrally
description: 用本机的 `wrally` 命令行工具在 WorkRally 上批量制作短视频（短番项目 → 剧集 → 场次）：新建或拉取一集、改提示词和台词并推送、批量生成关键帧 / 视频 / 旁白、选图、检查素材、导出给合成脚本、生成进度网页。只要任务涉及 WorkRally 上某一集或某几镜的提示词、关键帧、视频、旁白、场次、选图，或者目录里有 `episode.yaml`、`wrally.yaml`、`.wrally/`，就先用这个 skill，哪怕用户没提 wrally，也不要直接去调 WorkRally 的 MCP 工具。不属于任何一集、只想用 WorkRally 生成一张图、一段视频、一段配音或一段音乐时，也用它（`wrally gen`，不用建项目）。用户说「给第 5 镜重做视频」「这集的旁白生成一下」「看看这集进度」「把提示词推上去」「开新的一集」「用 wrally 开一部新片子」「用 WorkRally 生成一段配音/一张图」时都适用。
---

# wrally

`wrally` 是装在本机的全局命令，把 WorkRally 上一集短视频的批量操作收进脚本。一条命令处理一整集，只回几行摘要，明细写进文件。

**为什么不直接调 WorkRally 的 MCP 工具**：逐镜调用会把上下文撑满（每次返回都带签名地址和提示词全文），调用顺序错了还会悄悄生成出错的东西（比如没写单图配置就生成视频，画面不会参考关键帧）。这些顺序和坑 wrally 都内置了。所以凡是 wrally 有命令能做的事都用它；确实没有对应命令的操作，先告诉用户再决定。

## 先弄清自己在哪

| 目录里有 | 这是什么 | 怎么跑命令 |
| --- | --- | --- |
| `episode.yaml` | 单集目录（工作区） | 直接跑 |
| `wrally.yaml`，没有 `episode.yaml` | 项目目录，下面的子目录各是一集 | `wrally status`、`wrally page` 直接跑；其余命令加 `-C <单集目录>` |
| 都没有，只想单独生成几个文件 | 不需要项目 | 用 `wrally gen`，见下文「单独生成」 |
| 都没有，要做一集 | 还没建项目 | 读 `references/project-setup.md` |

先读这个目录里的项目说明文件（`CLAUDE.md`、`AGENTS.md` 或你所用 Agent 约定的同类文件，有哪个读哪个）。**片子专属的东西都在那里**：用哪些模型和音色、制作规则、怎么合成。这份说明只讲工具本身，两边冲突时以项目说明文件为准。

状态全在文件里，不靠对话记忆：`episode.yaml`（清单：每镜的引用和进度）、`prompts/`（每镜独有的提示词）、`.wrally/`（下载物和报告）。接手一集时先 `wrally status`。

## 命令一览

`--shots` 支持 `all`、`02,05`、`02-07`。所有命令都有 `--json`；调远端的命令有 `-v`。拿不准选项就 `wrally <命令> --help`。

| 命令 | 做什么 | 调远端 | 花额度 |
| --- | --- | --- | --- |
| `status` | 进度表；项目目录下每集一行 | 否 | 否 |
| `lint [--shots]` | 检查本地提示词和台词 | 否 | 否 |
| `check [--shots]` | 抽帧拼图、时长、音高、素材是否过期 → `.wrally/report.md` | 否 | 否 |
| `keyframe sheet [--shots]` | 候选关键帧对比图 → `.wrally/sheets/` | 否 | 否 |
| `keyframe pick 02 A 03 B …` | 选定关键帧（只改本地） | 否 | 否 |
| `export [--shots]` | 合成要用的输入 → `.wrally/export.json` | 否 | 否 |
| `page [--open] [--watch]` | 进度网页 `page.html`；项目目录下重建各集和总览页 | 否 | 否 |
| `pull [--force]` | 拉场次列表，建立或更新清单和 `prompts/` | 是 | 否 |
| `push [--shots] [--dry-run]` | 把本地提示词写到远端（只写不同的镜头） | 是，**覆盖远端** | 否 |
| `verify-picks [--shots]` | 核对清单选图和网页「选用」 | 是 | 否 |
| `wait <kind>`、`fetch <kind>` | 等任务完成；下载最新结果。`kind` 是 `keyframe`、`video`、`audio` | 是 | 否 |
| `new --shots 00-11,99 [--name …] [--dry-run]` | 新建剧集和场次并建好清单 | 是，**建远端数据** | 否 |
| `keyframe gen`、`video gen`、`audio gen` | 生成。都有 `--shots --dry-run --wait --fetch --redo`；**默认只提交**，要等待和下载加 `--wait --fetch` | 是 | **是** |
| `smoke [--only …] [--resume] [--dry-run]` | 真实调用一遍：依次生成图像、配音、5 秒视频并检查文件 | 是 | **是** |
| `login` | 浏览器授权（凭证过期时） | 是 | 否 |

`video gen` 另有 `--force-picks`，`audio gen` 另有 `--redo-failed`（只重做音高超标或上次失败的）。

## 一集的流程

每一步是一次 bash 调用。

1. **建档**。新的一集：`wrally new --shots 00-11,99 --name "EP5 …" --dry-run`，确认后去掉 `--dry-run`。可以先手写清单条目（不写 `story_id`）和 `prompts/` 再跑 `new`：条目按镜号认领，本地提示词保留，建完直接走第 2 步的 `lint` → `push`。远端已有场次的：`wrally pull --project <ID> --series <ID>`（有项目文件时不用给 `--project`）。
2. **写提示词和台词**。编辑 `prompts/<镜号>.image.md`、`.anim.md` 和清单里的 `narration`，然后 `wrally lint`，再 `wrally push --dry-run` → `wrally push`。
3. **关键帧**：`wrally keyframe gen --dry-run` → 用户确认 → `wrally keyframe gen --wait --fetch`。把 `.wrally/sheets/keyframes-<镜号>.jpg` 给用户看，等用户说选哪张，再 `wrally keyframe pick 02 A 03 B …`。
4. **视频**：`wrally video gen --dry-run` → 确认 → `wrally video gen --wait --fetch`。
5. **旁白**：`wrally audio gen --dry-run` → 确认 → `wrally audio gen --wait --fetch`。
6. **检查**：`wrally check`，只看有警告的镜头；需要看画面时读 `.wrally/sheets/<镜号>.jpg`（小图）。返修就改提示词或台词，重跑对应的 `gen`，用 `--shots` 限定范围。
7. **合成**：`wrally export`，然后运行这部片子自己的合成脚本（见项目说明文件），最后 `wrally page` 让进度网页带上成片。

## 提示词怎么写

发给 WorkRally 的提示词 = 这一镜独有的内容 + 全集共用的文本（画风后缀、动画前缀、旁白模板）。**本地文件里只写独有的部分**，共用文本由 wrally 拼接，改一处全集生效。

- 把完整提示词（含共用文本）贴进文件，`lint` 和 `push` 会报 `already_styled`。要么删掉共用的那段，要么在清单里给这一镜标 `raw: [image]`（或 `anim`）表示这一镜不拼接。
- 改台词只改清单里的 `narration`，`audio gen` 会自己拼好旁白提示词并写到远端，不用 `push`。
- 改了共用文本，所有拼接的镜头都会变成待 `push`，这是正常的。

## 几条要守的规矩

**生成前先演练并让用户确认。** `keyframe gen`、`video gen`、`audio gen` 消耗真实额度。先 `--dry-run` 看会提交哪些镜头，把镜号告诉用户，得到同意再提交。输入没变的镜头会自动跳过，所以整集重跑是安全的；`--redo` 会无条件重新生成，只在用户明确要重做时用，并用 `--shots` 限定。

**`push` 和 `new` 也先 `--dry-run`。** `push` 覆盖远端的提示词（写之前的原文会备份到 `.wrally/raw/history/`）；`new` 在远端建剧集和场次。

**提交超时不要重试。** 生成、建剧集、建场次超时后，服务端可能已经受理，重发会重复扣额度或建出重复的场次。先 `wrally wait <kind>` 或 `wrally status` 看实际状态，任务在跑就用 `wait`、`fetch` 接上。

**选图由用户决定。** 出对比图给用户看，不要替用户选。

**不要把签名地址和提示词全文贴进对话。** 命令的输出本来就不含它们；`.wrally/raw/` 里的原始返回两样都有，要查问题时只看结构，不要整段打印。

**用 wrally 制作时别同时在网页上编辑同一集。** 网页端保存会把文本改回旧版。

## 读懂输出

退出码：`0` 全部成功；`1` 部分镜头失败或等待超时，其余已完成；`2` 前置检查没过，什么都没提交；`3` 鉴权或网络错误；`4` wrally 自身的意外错误。

出错时标准错误里有一行 `error: <错误码>  <提示>`；带 `--json` 时标准输出另有一行 `{"error": …, "hint": …, "exit_code": …}`。

| 看到 | 意思和做法 |
| --- | --- |
| `pull` 里某列是 `local` | 本地和远端不同，本地的被保留了。自己刚改过、还没 `push` 属正常；不是自己改的就先告诉用户，弄清原因再 `push`（远端可能被网页端改回了旧版） |
| `web copy differs` / `web anim copy differs`，或生成时报 `stale_web_prompt` | 这一镜在网页上编辑过，远端另存着一份富文本副本，生成用的是它，而 wrally 改不到。**只能请用户**在网页上（先刷新页面）把提示词粘贴成和 `prompts/<镜号>.image.md` 或 `.anim.md` 一致。不要试着用接口去写，服务端会忽略 |
| `pick_conflict` | 网页上的「选用」和清单不一致。告诉用户两边各是哪张；用户确认以清单为准后用 `video gen --force-picks` |
| `missing_asset_id` | 这一镜还没选关键帧，先 `keyframe pick` |
| `no_manifest` | 当前目录不是单集目录。项目目录下加 `-C <单集目录>`；全新的目录先 `pull` 或 `new` |
| `check` 报 `… was made from different …` | 素材是用旧的输入做的（台词或提示词改过而没重做）。重跑对应的 `gen` |
| `unpushed_prompts` | 本地提示词和远端不一致，`keyframe gen`、`video gen` 不肯提交。先 `wrally push` |
| `login_expired`、`auth_failed`，退出码 3 | `wrally login`（会开浏览器，需要用户操作；浏览器授权后跳回本机 `127.0.0.1` 的端口，所以要在用户自己的电脑上跑，云端沙箱里跑不通） |
| `connect_failed`、`network_error`，退出码 3 | 网络问题，稍后重试；如果是在提交生成之后才断的，按上面「提交超时不要重试」处理 |
| `note: pitch not checked` | 没装量音高用的 librosa，其余检查照常 |
| `internal_error`，退出码 4 | wrally 自己的问题，追踪栈在凭证目录的 `last-crash.txt`（默认 `~/.config/wrally/`）。告诉用户，不要绕过 wrally 直接调接口。如果是提交之后出的错，任务可能已受理：先 `wait`、`status` 看状态，不要重新提交 |

`check` 查不出来的两件事：旁白可能念到一半就结束（时长明显偏短的要听一遍或用语音识别核对）；wrally 之外生成的旁白没有指纹，台词改了它不会报过期。

旁白偶尔只有一个声道有声音，音高和时长都正常。项目的 `rules` 设了 `channel_gap_max` 时 `check` 会报 `channel gap …dB`，`audio gen --redo-failed` 会重做；没设就查不出来。

旁白偶尔混进背景音乐或环境声。设了 `noise_floor_max`（负的 dB，如 -50）时 `check` 量最安静的连续 0.3 秒的电平：说话的停顿处应当接近无声，带背景音的会报 `noise floor …dB`，`--redo-failed` 同样会重做。同一句台词重做几次都带背景音时，改写台词往往比再生成管用。

`wait` 超时不代表失败，任务可能还在跑，再 `wait` 一次。`audio gen --wait` 在等待阶段崩溃过一次而任务其实已受理（现在报 `internal_error`、退出码 4）：遇到就用 `wait audio`、`fetch audio` 接上，不要重新提交。

**等待别超过自己执行命令的时限。** `wait` 默认最长等 1200 秒，生成命令的等待默认 600 到 1200 秒，而 Agent 执行一条命令往往只允许几分钟，命令被中途杀掉时什么摘要都拿不到。最省事的做法：设环境变量 `WRALLY_MAX_WAIT`（秒），比自己的时限短一些，它压住所有命令的 `--timeout`。也可以把提交和等待分开跑，两组命令的写法不同：

| 命令 | 只提交 | 之后接着等、接着下载 |
| --- | --- | --- |
| `keyframe gen`、`video gen`、`audio gen`（一集里的镜头） | 不加 `--wait`（默认就只提交） | `wrally wait <kind> --timeout <秒>`，完成后 `wrally fetch <kind>` |
| `gen image\|video\|audio\|music`（单独生成） | 加 `--no-wait` | `wrally gen fetch -o <目录> --timeout <秒>` |
| `smoke`（接口测试） | 不能只提交；`--timeout` 或 `WRALLY_MAX_WAIT` 到点就停 | `wrally smoke --resume` |

超时（退出码 1，表里是 `running`）就再跑一次等待的命令。命令被杀掉也不会弄坏状态，接着等即可，不要重新提交。

## 单独生成（不用项目和清单）

`wrally gen image|video|audio|music` 走 WorkRally 的画布生成接口，不建项目、剧集、场次，结果直接下载到 `-o` 指定的目录（默认当前目录）。

```
wrally models audio                                  # 先看可用模型和参数范围（image/video/audio/music）
wrally gen audio "使用参考音频的音色……他说：「……」" --model seed-audio-1.5 --voice wnw8ni23ax -o out --name line1 --dry-run
wrally gen image --prompt-file p.md --model "Seedream-5 Pro" --aspect 16:9 --resolution 4 --count 2 [--ref <媒资ID>] -o out
wrally gen video "推镜……" --model "Seedance-2.0 Mini" --mode firstlast --image <媒资ID> --last <媒资ID> --duration 8 --resolution 1 -o out
wrally gen music "钢琴，舒缓" --model MiniMax-music3.0 [--lyrics-file 歌词.txt | --ai-lyrics] -o out
wrally gen fetch -o out                              # 超时、--no-wait 或下载中断后接着等、接着下载
```

- `--model` 填 ID 或名称都行；分辨率、宽高比、时长、张数、参考音色数会按模型目录校验，不合法时退出码 2，什么都不提交。
- 参考图、首尾帧、参考音色都只收**媒资 ID**；本地文件还不能直接传（要先在网页上传入库）。
- **默认提交后等待并下载**，和上面一集的生成命令正好相反（那边默认只提交、要加 `--wait --fetch`）。这里不用加 `--wait`（加了也一样）；`--no-wait` 只提交，之后用 `gen fetch` 接上。`--project <短番项目ID>`（或环境变量 `WRALLY_PROJECT`）让结果归到那个项目的媒资库，不给也能生成。
- 视频默认不生成音效，要音效加 `--sound`（远端接口默认是开的，wrally 一律显式写）。
- 提交记录在输出目录的 `.wrally-gen.json`。同样的参数再跑会跳过（要再出一批加 `--again`，文件名带 `-b2`）；提交超时的记录停在 `submitting`，再跑会报 `pending_submit`：先去网页确认是否已受理，确认没有再 `--again`。任务失败时表格显示 `failed: <服务端给的原因>`；「现在使用的人比较多」这类是远端繁忙，过会儿用同样的命令再跑即可（整条记录都失败时不挡重跑，不用 `--again`；`--count` 大于 1 只失败一部分时要加 `--again`）。
- 同样消耗额度：先 `--dry-run`，把模型和份数告诉用户，确认后再提交。

## 测试接口是否可用（`wrally smoke`）

用户想确认 WorkRally 的生成接口能不能用、某个模型能不能跑通时，用 `wrally smoke`。它依次提交三个最小的任务并检查下载的文件：一张图（模型支持的最低分辨率）、一段配音（项目设了 `voice_asset` 时带上参考音色）、一段 5 秒视频（最低分辨率，不带音效，用前一步生成的图当单图；图像失败时改用纯文字）。

```
wrally smoke --dry-run                     # 列出三步的模型和参数，不提交
wrally smoke                               # 依次跑图像 → 配音 → 视频
wrally smoke --resume                      # 接着最近一次运行：已提交的不重复提交
wrally smoke --only image,audio            # 只跑其中几步
wrally smoke --video-model "Seedance-2.0 Mini" --dry-run   # 换模型
```

- 模型依次取：`--image-model`、`--audio-model`、`--video-model` 选项；`wrally.yaml` 和清单合并后的 `defaults.<kind>.model`；内置默认（Seedream-5 Pro、Seed-Audio-1.0、Seedance-2.0 Mini）。所以在任何目录都能直接跑。`--dry-run` 的模型列标出来源（`option`、`defaults`、`built-in`）。内置模型不在远端模型目录里时报 `missing_model`，不自动换别的，用选项指定一个（`wrally models <kind>` 看有哪些）。视频模型没有 5 秒选项时报 `bad_duration`。这些都在提交任何任务前检查，退出码 2。
- 每次运行在当前目录建 `.wrally-smoke/<时间戳>/`，里面有下载的 `image.*`、`audio.*`、`video.mp4`，计划和进度在 `smoke.json`，提交记录是 `.wrally-gen.json`（和 `wrally gen` 相同）。
- 检查：图像要读得出宽高，配音要有时长，视频要有画面且时长在 5±0.5 秒；不通过显示 `failed: check`。服务端拒绝某一步（`failed: <错误码>`）不影响后面的步骤。
- 某一步失败（比如「现在使用的人比较多」）时，过会儿 `wrally smoke --resume` 会只重新提交失败的那一步，失败的任务没有扣额度。
- 等待超时时，这一步显示 `running`，后面的显示 `not started`，退出码 1；用 `wrally smoke --resume` 接上，不要不加 `--resume` 重跑（那会新开一次运行，重新提交三个任务）。提交时网络超时会在 `--resume` 时报 `pending_submit`：先去网页确认任务是否已受理，确认没有再不加 `--resume` 新开一次。
- 消耗真实额度：先 `--dry-run`，把三步的模型告诉用户，确认后再跑。

## 更多

- 开一部新片子、写 `wrally.yaml`、清单字段、给片子写合成脚本要读的 `export.json`：读 `references/project-setup.md`。
- 工具的源码和开发说明（`CLAUDE.md`）在 wrally-cli 仓库（`https://github.com/capken/wrally-cli`；本机用 `--editable` 装的，改的就是它的本地副本）。要改工具本身的行为到那里去改，不要在创作项目里绕过它。
