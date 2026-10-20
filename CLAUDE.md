# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## wrally 是什么

本地命令行工具（包名 `wrally-cli`，命令叫 `wrally`），把 WorkRally 上「一集短视频」（短番项目 → 剧集 → 场次）的批量操作收进脚本：关键帧 → 视频 → 旁白，最后把合成需要的输入导出给合成脚本。目的是让 Claude 只通过 bash 调 `wrally` 并看到几行摘要，而不是逐镜调用 WorkRally MCP 把上下文撑爆。

它不绑定任何一部片子：版式、字体、阈值、词表都不在这里。每部片子是一个创作项目目录（项目文件 `wrally.yaml` 加各集的工作区），有自己的合成脚本和自己的 `CLAUDE.md`。目前唯一的创作项目是《提灯人》，在 `/Users/allenzheng/codes/lantern-bearer`，改了行为要拿它做回归。

明确不做：通用 WorkRally SDK；替人选图；剪辑合成（wrally 不调用合成脚本、不解析它的输出，只用 `wrally export` 写一份中性的 `.wrally/export.json`）。

`WorkRally 批量命令行工具（wrally）设计文档.md` 是规格，描述的是**现在的实际行为**：清单字段、每条命令的调用顺序、规则与坑、待确认问题都在里面。改行为时同步更新它。`refactor.md` 是从旧仓库 `capken/wrally` 拆出来的计划和记录（四步都已完成，旧仓库和旧目录还没舍弃）。

## 仓库里有什么

- `src/wrally/`：代码。
- `tests/`：单元测试，全部离线，用 `tests/conftest.py` 里的 `FakeWorkRally` 模拟远端。
- 仓库里没有任何一部片子的工作区、提示词或素材，也不要放进来。

## 常用命令

用 `uv` 管理环境（Python 3.11，版本写在 `.python-version`）。本机系统的 `python3` 是 3.9，一律通过 `uv run`。

```
uv sync                                          # 安装依赖（dev 组里带着可选的 librosa）
uv run pytest -q                                 # 全部测试
uv run pytest tests/test_pull.py -k idempotent   # 单个测试
uv run ruff check . && uv run ruff format .      # lint 与格式化

uv run wrally login                              # 浏览器授权，凭证存 ~/.config/wrally/
uv run wrally status -C <单集目录>               # 命令在单集目录下运行，或用 -C 指定
uv run wrally new --project <ID> --name "EP4 …" --shots 00-11,99 --dry-run   # 新建剧集和场次
uv run wrally video gen --shots 02-05 --dry-run  # 生成命令先演练
uv run wrally page --open                        # 生成进度网页 page.html 并打开；--watch 守着改动
uv run wrally page -C <项目目录>                 # 重建各集页面和总览页 index.html
uv run wrally export -C <单集目录>               # 写 .wrally/export.json，给合成脚本用
```

本机已把这份源码装成全局命令 `wrally`（`uv tool install --editable …[pitch]`，2026-10-13），在创作项目里直接敲 `wrally`，源码改了立即生效；改了依赖要重装，命令在 `refactor.md` 第 4 步。这台机器有时解析不了 `pypi.org`，`uv` 要下载而连不上时加 `--default-index https://pypi.tuna.tsinghua.edu.cn/simple`（清华镜像，用户同意过），或在缓存够用时加 `--offline`。

环境变量：`WRALLY_CONFIG_DIR`（凭证目录）、`WRALLY_MCP_URL`（MCP 地址）、`WRALLY_CONCURRENCY`（查询和下载的并发数，默认 4）。

外部依赖：`ffmpeg`、`ffprobe`（抽帧、时长）。量音高要 `librosa`，是可选依赖 `pitch`。

## 架构

四层，自下而上依次调用，只有命令层与人或 Claude 交互：

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 传输 | `transport/auth.py`、`client.py` | OAuth 登录与刷新；调用 MCP 工具、重试、解析、把原始返回落到 `.wrally/raw/` |
| 接口 | `api/` | 把 WorkRally 工具包装成有类型的函数；**下载地址只在这一层流转** |
| 命令 | `commands/`、`cli.py` | 读写清单、编排调用顺序、输出摘要 |
| 工作区 | `workspace.py`、`prompts.py`、`rules.py` | `episode.yaml` + `prompts/` + `styles/` + `.wrally/`，唯一的状态来源；上级目录的 `wrally.yaml` 提供各集共用的设置 |

不涉及远端的辅助模块：`media.py`（图像比对、抽帧、音高）、`output.py`（表格和输出预算）。

命令与模块的对应：`new` `pull` `push` `fetch` `wait` `status` `check` `lint` `export` `page` 各一个同名文件；`verify-picks` 在 `verify.py`；`keyframe pick/sheet` 在 `keyframe.py`，`keyframe gen` 在 `keyframe_gen.py`；`video gen` 在 `video.py`；`audio gen` 在 `audio.py`。`commands/common.py` 放镜头选择、并发执行、清单小节的读写。

## 写任何命令都要满足的约束

- **输出有预算**：一行结论 + 不超过 15 行的表，总长不超过 30 行（`output.render` 负责截断）；明细写文件。`--json` 和 `-v` 同样不含 URL、提示词全文和返回体。中文直接 UTF-8 输出。
- **文件即状态**：命令只依赖清单和 `prompts/`，不依赖对话记忆。`status` `check` `lint` `keyframe pick/sheet` `export` `page` 不调远端。
- **幂等可重跑**：输入没变不重复提交，已下载的不重复下载，重跑 `pull` 不产生改动。写文件用 `workspace.write_if_changed`（内容相同不写，原子替换）。
- **单镜错误不中断其他镜头**（`common.for_each`），结束后统一汇总；鉴权错误除外。远端返回的错误只在终端打一行错误码和原始返回的路径。
- **退出码**：0 全部成功；1 部分失败或超时；2 前置检查不通过，未提交任何任务；3 鉴权或网络错误。用 `errors.py` 里对应的异常类型。
- **下载地址不出接口层**：`api/models.py` 的 `strip_urls` 在解析时丢弃所有 URL；`Asset.url` 不参与序列化和打印。命令层只拿 asset_id 和本地路径。
- **提交生成任务、新建剧集和新建场次的调用传 `retry=False`**：超时后无法判断服务端是否已受理，自动重发可能重复消耗额度或建出重复的剧集和场次。
- **不写进某一部片子的东西**：阈值、词表、版式、字体、合成参数都属于创作项目。代码里出现片名或某一集的经验值就是放错了地方。

## 鉴权

WorkRally MCP 是标准的 MCP OAuth 2.1（DCR + 授权码 + PKCE），允许 Claude 以外的客户端连接。

- **OAuth 是自己实现的（`transport/auth.py`），`mcp` SDK 只负责会话。** SDK 自带的 `OAuthClientProvider` 在新进程里不知道 token 何时过期，收到 401 会直接弹浏览器授权而不是刷新；CLI 每条命令都是新进程，所以不能用它。
- access_token 4 小时过期；refresh_token **每次刷新都会轮换**。刷新必须在文件锁内进行并立即原子写回，刷新请求不能重试。
- 凭证在 `~/.config/wrally/credentials.json`（0600），不进工作区、不提交。
- HTTP 库用 `httpx2`（SDK 2.x 依赖的），不要再引入 `httpx`。

## 容易做错的地方

设计文档「必须内置的规则与坑」有完整表格，新增规则要配单元测试。下面是改代码时最容易踩的：

**调用顺序和接口选择**

- `edit_content` 必须在 `set_model` 之前，并且始终带 `series_id`。
- 查场次只用 `shotlist_manage list`；`get` 返回空提示词，接口层不要暴露它。
- 视频走单图模式，配置里叫 `mode: Text`，必须和 `textModel`、singleAssets（带 asset_id）一起每镜完整写入。远端已有的配置不可信，生成前一律按清单重写。
- 旁白：逐镜 `bind_asset`（已绑着同一个音色的跳过）→ 批量 `edit_content` → 一次 `set_model audio`（带模型的 capabilities）→ 提交。绑定用的名字必须和提示词里的 `<音色名>` 一致。
- singleAssets 和 `bind_asset` 里的地址用 `api.assets.plain_url`：`asset_detail` 的短链接跳转后去掉查询串。

**清单和提示词**

- `defaults`、`rules` 一律用 `ws.settings(data, 名字)` 读，它把项目文件和单集清单合并好；直接读 `data.get("defaults")` 会漏掉项目文件里的值。合并结果只用来读，不要写回清单。
- 清单顶层和每镜里 wrally 不认识的键（比如《提灯人》的 `assemble`）由 `export` 原样带进 `extra`，wrally 不解释，也不解析里面的路径。认识的键列在 `commands/export.py` 的 `TOP_KEYS`、`SHOT_KEYS`：给清单加新字段时要补进去，否则会被当成 `extra` 带出去。
- 成片文件的位置是清单顶层的 `output`（默认 `<目录名>.mp4`），用 `ws.output_path(data)` 取；`export` 和 `page` 都用它。
- `check`、`lint`、`audio gen --redo-failed` 的阈值和词表取自 `rules.load_rules`，不要在命令里再写常数。`rules` 没有缺省值：没写的一项不查。测试里要用规则就在夹具里写 `data["rules"]`。
- 候选和结果以 asset_id 为主键，任务 ID 只是附属信息。`get_result` 不返回任务 ID；`doing_tasks` 里的 ID 是另一种格式，不要拿来对应。
- `keyframe.candidates` 只追加：已有候选保持原位，否则候选序号会错位。
- 本地只存每镜独有的提示词，公共文本由 `prompts.py` 拼接；与远端比较一律用 `prompts.composed`。偏离公共文本的镜头标 `raw`。`pull` 只在「拼回去逐字相同」时才拆分，不要改成模糊匹配。
- 音高参数（`media.median_f0`）不要改：各部片子的 `rules.f0_max` 是按这组参数定的。`librosa` 是可选依赖（`pitch`，开发环境里随 dev 组装着）；只在设了 `f0_max` 时才量，没装就跳过并留一行 `note`。
- wrally 只数整段台词的字数（`rules.core_len`，不计标点）。逐条字幕的断句和长短是合成脚本的事，wrally 里没有对应的代码，`lint` 也不查。
- `lint` 的关键词检查只看每镜自己写的部分，并跳过否定写法（「不出现 A、B」和「不碰杯」都算）。

**进度网页**

- `page` 生成单集目录下的 `page.html`（模板是 `src/wrally/page_template.html`）。会改清单或下载素材的命令都挂了 `cli.refreshes_page`，结束后自动重建；新增这类命令时也要挂上。它不能往标准输出写东西，也不能改变原命令的退出码。
- 有项目文件时，每次重建单集页面都会顺带重建项目总览页 `index.html`（模板 `index_template.html`）。总览页只读各集的 `.wrally/page.summary.json`，卡片上要加新的数就加到 `page.summarize` 里。
- 成片是合成脚本在 wrally 之外写的，合成完页面不会自己更新：跑一次 `wrally page`。
- 页面内容只由输入决定，不要往里写时间戳，否则重跑会产生改动、开着的页面会不停刷新。
- 页面文字一律由脚本用 `textContent` 写入，不要改成拼 HTML 字符串：提示词是任意文本。
- 页面上「需在网页上改」「网页副本不同」的标识来自 `.wrally/web_copies.json`，由 `common.remember_web_copies` 在每次拿到完整场次列表时整份重写。新增会取场次列表的命令时也调一下它；只传完整的列表，传一部分会把其余镜头的记录清掉。
- 素材状态的判断在 `page.py`，过期判断复用 `check.stale`；`status` 另有一套更粗的文字状态，改状态含义时两边都看一下。

**生成命令**

- 三个生成命令（`keyframe_gen.py`、`video.py`、`audio.py`）的「分类 → 恢复 → 落盘 submitting → 提交」骨架是各写一份的，改其中一个的幂等逻辑时要同步另外两个。
- 幂等靠清单里的 `made_from`（输入指纹）。提交前先把状态落盘为 `submitting` 并记下 `results_before`、`failed_before`；`wait` 也靠这组基线判断，避免把旧结果当成新任务的结果。
- `keyframe gen`、`video gen` 要求提示词已经 `push`；`audio gen` 自己写旁白提示词。
- `video gen` 用 `keyframe.synced`（wrally 上次写进远端的选用图）判断网页上是否被人改过。
- 单镜要换视频时长就在清单里给这一镜写 `video_duration`，要换分辨率就写 `video_resolution`（Seedance-2.0 Mini：1 是 480p、3 是 720p），不要改 `defaults.video` 里的对应值：它们计入每镜的输入指纹，改默认值会让整集的视频都被判为输入变了。指纹一律经 `video.shot_settings` 取参数。

**远端不可靠的地方**

- 远端的文本会被 wrally 之外的写入改回旧版（推测是网页端开着旧页面时保存造成的，未证实）。`pull` 出现意外的 `local` 时先弄清原因再 `push`。
- 旁白可能念到一半就结束，`check` 查不出来（《提灯人》EP4 的 08 镜只念了前半句）。
- `fetch` 取的是最新一条结果，`export` 带出的台词（合成时的字幕）取清单里的 `narration`，两者没有互相校验。wrally 生成的素材靠指纹由 `check` 把关；wrally 之外生成的旁白查不出来。
- `asset_detail` 对不存在的 ID 静默丢弃，不报错。
- 在网页上编辑过图片提示词的镜头，远端另有一份富文本副本（`extra.image_prompt_json`），场次生图用的是它，而 `edit_content` 只改纯文本，改不到它。`pull` 标 `web copy differs`，`push` 会列出镜号，`keyframe gen` 报 `stale_web_prompt` 中止。唯一的办法是请用户在网页上把提示词粘贴成和 `prompts/<镜号>.image.md` 一致；不要尝试用接口写这个字段（试过，服务端忽略）。
- 动画提示词也有同样的网页副本（`extra.animation_prompt_json`），场次生视频用的是它；用户在网页上保存图片提示词时会顺带把当时的动画文本存进去。`pull` 标 `web anim copy differs`，`push` 会列出镜号，`video gen` 报 `stale_web_prompt` 中止。同样只能请用户在网页上粘贴成和 `prompts/<镜号>.anim.md` 一致，粘贴前先刷新页面。

## 还没验证或没做的

真实验证都是在《提灯人》的四集上做的（EP3 走过全流程，EP4 用 `new` 建，EP2 用 `pull` 建档后返修）。

- `new` 真实建过一集（剧集加 13 镜）；给已有剧集加镜头、建场次超时后重跑只有单元测试。
- 带 `refs` 的画布路径（`canvas_generate_image`）只有单元测试，没有真实提交过。
- 中断恢复、绑定音色、多镜一起提交时任务 ID 的对应，只有单元测试。选图冲突（`pick_conflict` 加 `--force-picks`）和多镜一起提交真实跑过。
- 富文本副本的三处检查（`web copy differs`、`push` 的提示、`stale_web_prompt`）只有单元测试；真实只确认了两份一致时不误报。动画副本的三处检查同样只有单元测试。
- 每镜视频时长 `video_duration` 除单元测试外只真实用过一镜。
- `McpClient` 的断线重连没有测试。
- 每镜视频分辨率 `video_resolution`（2026-10-20 加）只有单元测试。
- `web_copies.json`：四集现有的记录是用 `.wrally/raw/` 里最近一次场次列表补出来的，没有经过真实的 `pull`；`pull`、`push`、生成命令写它只有单元测试。
- `page`：开着的页面自动刷新（定时加载 `.wrally/page.version.js`）、侧栏随滚动高亮、`J`/`K` 跳镜和 `--watch` 没有在真实浏览器里实测；深色模式和窄屏只用无界面 Chrome 看过一眼。
- `audio gen --wait` 崩过一次（2026-10-06）：13 镜提交成功后，等待阶段从 `common.for_each` 抛出未处理的 `CancelledError`，打出整段追踪栈，退出码 1。原因没查明。任务其实已受理，用 `wait audio`、`fetch audio` 接上即可，不要重新提交。
- 项目文件、`rules`、`export` 和这次拆分之后，调用远端的命令只跑过 `pull`（2026-10-13，四集零改动）；`push` 和三个生成命令还没跑过。离线的 `status`、`lint`、`check`、`page`、`export` 在四集上逐一对比过。
- `export` 里素材缺失或过期的标记、没装 `librosa` 时跳过音高，只有单元测试。
- 通用化的后续待办（通用模板、多音色等）在设计文档「后续待办：通用化」；视频超分在 `refactor.md` 末尾。
- 没有做：用语音识别核对旁白与台词；指定使用非最新的那条结果。

## 提交

单人仓库，直接提交到 `main`，不另开分支；推送要用户明确要求才做。

## 在真实剧集上操作时

- 生成类命令（`keyframe gen`、`video gen`、`audio gen`）消耗真实额度：先 `--dry-run`，提交前先向用户确认。
- `push` 会覆盖远端的提示词：先 `--dry-run`。写之前的远端原文会自动备份到 `.wrally/raw/history/`。
- 用哪些模型由创作项目的 `wrally.yaml` 和清单合并后的 `defaults` 决定，远端场次里已存的模型不一定是这些。
- 不要把签名 URL 或提示词全文贴进对话或提交进仓库。`.wrally/raw/` 里的原始返回两样都有，只看结构、不要整段打印。
