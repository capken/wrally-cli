---
name: verify-wrally
description: 像用户一样驱动 wrally 命令行（本仓库的源码），证明一处改动真的生效：在一个离线的夹具创作项目里跑 status、lint、check、export、page、keyframe pick/sheet，留下终端记录、写出的文件和页面截图。改了命令的行为、输出、退出码、清单字段、export.json 或进度网页之后，单元测试通过了还想看真实命令的表现时用它。
---

# 验证 wrally

wrally 是命令行工具，没有常驻进程。用户接触到的是：终端里的几行摘要、退出码、工作区里被改写的文件（`episode.yaml`、`.wrally/` 下的报告和 `export.json`）、浏览器里的 `page.html` 和 `index.html`。验证就是在一个真实的工作区目录里敲真实的命令，再看这四样。

单元测试（`uv run pytest -q`）用 `FakeWorkRally` 直接调命令层的函数，不经过 `cli.py`。这个 skill 补的是那一段：选项解析、输出预算、退出码、`refreshes_page` 的自动重建、多条命令连起来的效果。

脚本都在 `.claude/skills/verify-wrally/scripts/`，下文用 `S` 指这个目录。一律用 `bash "$S/<脚本>"` 运行。

## Launch

没有要启动的服务。准备两样东西：

1. 依赖：在仓库根目录 `uv sync`（连不上 PyPI 时的办法见 `CLAUDE.md`）。
2. 夹具：`RUN=$(bash "$S/fixture.sh")`。它在 `${TMPDIR:-/tmp}/wrally-verify/<时间戳>/` 下建好一次运行用的目录并打印路径；也可以把目录作为参数传进去。

```
$RUN/project/            夹具创作项目，Cleanup 时删
  wrally.yaml            项目文件：两段公共文本、rules（slow_max 1.25、narration_max 12、一组词表）、一个 wrally 不认识的 assemble 小节
  styles/
  ep1/episode.yaml       一集三镜
  ep1/prompts/           每镜的图片和动画提示词
  ep1/.wrally/           ffmpeg 合成的 4 秒视频、3 秒和 6 秒旁白、四张纯色候选图
$RUN/cfg/                空的凭证目录
$RUN/evidence/           证据，Cleanup 不删
```

三镜各有用途：`01` 三类素材齐全、已选 A；`02` 旁白 6 秒配 4 秒视频、台词 15 字、动画提示词里有词表里的「写字」、有两张候选但没选；`03` 只有提示词。夹具里的 ID 都是假的，任何调远端的命令在它上面都跑不通，这是有意的。

就绪的标志：`fixture.sh` 退出码 0 并打印出目录。两次运行各用各的目录，可以并行。

## Doctor

```
bash "$S/doctor.sh"            # uv、ffmpeg、ffprobe 在不在；wrally 是不是从这个仓库的 src/ 导入的；当前提交
bash "$S/doctor.sh" --remote   # 另外用本机凭证调一次只读的 wrally projects
```

全是只读检查，退出码 0 才值得往下驱动。`wrally imports from` 那一行不是本仓库的 `src/wrally` 时，你驱动的不是你改的代码。只有要验证调远端的命令时才加 `--remote`。

## Drive

每条命令都经过 `drive.sh`：

```
bash "$S/drive.sh" "$RUN" <记录名> <wrally 的参数…>
bash "$S/drive.sh" "$RUN" status status -C "$RUN/project/ep1"
```

它跑的是这份源码（`uv run --project <仓库> wrally`，设了 `WRALLY_BIN` 就跑那个可执行文件），把命令、标准输出和标准错误、退出码写进 `$RUN/evidence/<记录名>.txt` 并打印出来；脚本自己的退出码就是 wrally 的。凭证目录被指向空的 `$RUN/cfg`，所以离线命令照常跑，调远端的命令以 `not_logged_in`、退出码 3 结束，碰不到本机凭证。

每个功能怎么驱动、看到什么算对，在 [`features/`](features/README.md)：先读索引，再读对应的那一篇。改动落在哪个功能，就驱动那一篇列出的全部入口，不要只挑顺手的一个。

要验证的行为夹具里没有（比如一个新的清单字段），就在 `$RUN/project/` 里像用户那样手改 `episode.yaml` 或 `wrally.yaml` 再驱动，并把改动前后的清单各存一份到 `$RUN/evidence/`。这类场景值得长期保留时，改 `fixture.sh` 并更新功能地图里的预期输出。

页面截图用无界面 Chrome（macOS 上的路径；别的系统换成对应的可执行文件）：

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --user-data-dir="$RUN/chrome-profile" --window-size=1400,1800 --virtual-time-budget=3000 \
  --screenshot="$RUN/evidence/page.png" "file://$RUN/project/ep1/page.html"
```

然后用 Read 工具看这张图。

### 调远端的命令

夹具验证不了它们。需要时在一个真实的创作项目里做（作者本机用哪一个记在 `CLAUDE.local.md`），不经过 `drive.sh`，因为要用本机的凭证；规则在 `CLAUDE.md`「在真实剧集上操作时」和 [`features/remote.md`](features/remote.md)。生成类命令消耗真实额度，`push` 覆盖远端提示词：只跑 `--dry-run`，真的提交前先问用户。

## Evidence

证据都放在 `$RUN/evidence/`：

- 每条命令的终端记录（`drive.sh` 自动写）。
- 命令写出的文件的副本：`export.json`、`report.md`、`lint.md`、`page.summary.json`，以及改动前后的 `episode.yaml`。它们在 `$RUN/project/` 里，Cleanup 会删，所以要复制出来。
- 页面截图。

标准：

- 走用户的路径，也就是命令行。不要直接调 `wrally.commands.*` 里的函数，那是单元测试的做法。
- 动作和结果都要有：命令的记录，加上它改动的文件在改动前后的样子。只有最后一屏不算。
- 核对副作用。命令说 `-> .wrally/export.json (updated)`，就打开那个文件看；说没改，就比对文件确实没变。
- 重跑一次同样的命令，应该报 `unchanged` 且文件不变（幂等是这个工具的约束）。
- `--dry-run` 跳过了什么要靠观察，不靠名字：`video upscale --dry-run` 就会调一次只读的远端接口。看清单和 `.wrally/raw/` 在前后有没有变化。
- 夹具上通过只说明离线路径没问题。调远端的路径没有在真实项目上跑过，就在报告里写「未验证」。

## Cleanup

```
rm -rf "$RUN/project" "$RUN/cfg" "$RUN/chrome-profile"
```

只删这次运行自己建的目录。`$RUN/evidence/` 留着，报告里给出它的完整路径。删完确认 `ls "$RUN/evidence"` 里的东西还在。没有要杀的进程；跑过 `wrally page --watch` 的话，结束你自己启动的那一个，不要按进程名杀。

## Helpers

| 脚本 | 用法 | 作用 |
| --- | --- | --- |
| `fixture.sh` | `RUN=$(bash "$S/fixture.sh" [目录])` | 建夹具，打印运行目录；目录里已有 `project/` 时拒绝并以 2 退出 |
| `doctor.sh` | `bash "$S/doctor.sh" [--remote]` | 只读检查，有一项不过就以 1 退出 |
| `drive.sh` | `bash "$S/drive.sh" "$RUN" <记录名> <wrally 参数…>` | 跑一条命令并留记录，退出码同 wrally |
| `env.sh` | 被上面三个引用，不单独运行 | 定位仓库，决定驱动哪个 wrally |

## 维护

命令、选项、输出格式或清单字段变了，功能地图里的预期输出会过时：用 `/pstack:maintain-verification-skill` 对一遍。
