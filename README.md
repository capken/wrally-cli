# wrally

`wrally` 是一个本地命令行工具，把 [WorkRally](https://workrally.qq.com) 上「一集短视频」（短番项目 → 剧集 → 场次）的批量操作收进脚本里完成：关键帧 → 视频 → 旁白 → 检查 → 导出给合成脚本。它主要给能执行终端命令的 AI Agent 用（Claude Code、Codex CLI、Gemini CLI、Cursor、WorkBuddy 等都行，不绑定任何一家），人在终端里也能直接用。

## 解决什么问题

让 Agent 直接逐镜调用 WorkRally 的 MCP 工具做一集十几镜的片子，会遇到三个问题：

| 问题 | 表现 | wrally 的做法 |
| --- | --- | --- |
| 上下文膨胀 | 返回里带签名下载地址和提示词全文，字段不能挑选。一集 14 镜的场次列表一次约 10 万字符，其中签名地址约 4 万；查一镜视频结果，一条地址就有 1.2k 字符；单图视频的模型列表约 2.8 万字符；54 个工具的定义合计约 9 万字符。会话后半段旧内容被清理，步骤要重做 | 一条命令处理一整集，终端只打一行结论加一张小表（不超过 30 行）；地址、提示词全文、原始返回只落在本地文件里 |
| 调用次数太多 | 查结果每次只能查一镜，关键帧、视频、旁白还要分开查，14 镜轮询一轮就是 14 次；绑定音色一次一镜；单图视频每镜的参考图不同，配置也得逐镜写。一轮几十次工具调用 | 在工具内部批量、并发调用；一集从关键帧到成片，Agent 侧大约十几次 bash 调用 |
| 流程里的坑靠人记 | 调用顺序写反导致「未配置模型」；视频没写单图配置就生成，画面不参考关键帧；查单个场次的接口返回空提示词；网页上编辑过的镜头另存一份提示词副本，接口改不到；选图记错镜号 | 调用顺序、公共提示词拼接、单图配置、网页副本检查、选图核对、素材过期检查都写成了默认行为 |

上面的数字是 2026-10-31 对 WorkRally MCP 实测的（工具定义，以及一集 14 镜的只读调用）。工具说明里已经加了一些提示（生视频前要确认、建议传剧集 ID 等），但这三个问题都还在。

另外几条设计保证它适合被 Agent 反复调用：

- **文件即状态**：一集的全部状态在 `episode.yaml`（清单）和 `prompts/` 里，换个会话也能接着做，不依赖对话记忆。
- **幂等可重跑**：输入没变的不重复提交，已下载的不重复下载；中途失败，重跑同一条命令就从断点继续。
- **退出码有含义**：`0` 全部成功；`1` 部分失败或等待超时；`2` 前置检查没过，什么都没提交；`3` 鉴权或网络错误；`4` wrally 自身的意外错误（不打追踪栈，追踪栈写进凭证目录的 `last-crash.txt`）。
- **机器可读**：每条命令都有 `--json`，成功时输出一行 `{"summary": …, "rows": [...]}`，出错时输出一行 `{"error": …, "hint": …, "exit_code": …}`。

它**不做**的事：不替人选图；不做剪辑合成（`wrally export` 只写一份中性的 `.wrally/export.json`，由每部片子自己的合成脚本使用）；不含任何一部片子的画风、字体、阈值和词表，这些都写在各自的创作项目目录里。

不属于任何一集、只想生成一张图、一段视频、一段配音或音乐时，用 `wrally gen`，不需要建项目。

## 安装

需要：

- [uv](https://docs.astral.sh/uv/)（会自动准备 Python 3.11）
- `ffmpeg` 和 `ffprobe`（抽帧、量时长和音量）：macOS 上 `brew install ffmpeg`
- 一个能访问 WorkRally 的账号

```bash
git clone git@github.com:capken/wrally-cli.git
cd wrally-cli

# 装成全局命令 wrally；--editable 让源码改动立即生效，[pitch] 带上量旁白音高用的 librosa（可选）
uv tool install --python 3.11 --editable ".[pitch]"

# 浏览器授权，凭证存在 ~/.config/wrally/credentials.json
wrally login
```

`wrally login` 会打开浏览器，授权后跳回本机 `127.0.0.1` 上的一个端口，所以要在有浏览器的电脑上跑。云端沙箱里的 Agent（Codex Cloud、远程容器等）收不到这个回调，目前没有专门的办法；能做的是在本机登录后，把 `credentials.json` 放到沙箱里，用 `WRALLY_CONFIG_DIR` 指过去。注意每次刷新 token 都会换一个新的 refresh_token，所以同一份凭证不要在两处同时用，否则先刷新的一边会让另一边失效，只能重新登录。

不想装全局命令，也可以在仓库里用 `uv sync` 后通过 `uv run wrally …` 运行。连不上 PyPI 时给 `uv` 加 `--default-index <镜像地址>`。

可选的环境变量：

| 变量 | 作用 |
| --- | --- |
| `WRALLY_CONFIG_DIR` | 凭证目录，默认 `~/.config/wrally/` |
| `WRALLY_MCP_URL` | WorkRally MCP 地址，默认 `https://workrally.qq.com/zenstudio/api/mcp` |
| `WRALLY_CONCURRENCY` | 查询和下载的并发数，默认 4 |
| `WRALLY_PROJECT` | `wrally gen` 的默认短番项目 ID |
| `WRALLY_MAX_WAIT` | 所有等待的上限（秒），压住各命令的 `--timeout`。Agent 执行一条命令有时限时，设成比时限短一些 |

## 在 Agent 中使用

### 1. 让 Agent 知道怎么用

使用说明在 `skills/wrally/SKILL.md`（细节在 `skills/wrally/references/project-setup.md`）：有哪些命令、一集的标准流程、哪些操作花额度要先确认、各种报错怎么处理。说明本身不依赖哪一家 Agent，按所用的 Agent 接进去：

| Agent | 接入方式 |
| --- | --- |
| Claude Code | 挂成 skill：`ln -s "$(pwd)/skills/wrally" ~/.claude/skills/wrally`。之后在任何目录，任务涉及 WorkRally 的剧集、场次、提示词、关键帧、视频、旁白，或目录里有 `episode.yaml`、`wrally.yaml` 时，会自动按它来 |
| Codex CLI 等读 `AGENTS.md` 的 | 在创作项目的 `AGENTS.md`（或全局的 `~/.codex/AGENTS.md`）里写一句：「WorkRally 的操作一律用 `wrally` 命令，动手前先读 `<wrally-cli 路径>/skills/wrally/SKILL.md`」 |
| WorkBuddy | 复制成 skill，另外要放开一个沙箱目录，见下文「在 WorkBuddy 里使用」 |
| Gemini CLI、Cursor 等 | 同理，写进它们各自读的说明文件（`GEMINI.md`、`.cursor/rules/` 等），或直接把 `SKILL.md` 的正文放进去 |

不管哪种 Agent，要点都一样：只通过 bash 调 `wrally`，看退出码和那几行摘要，不要直接调用 WorkRally 的 MCP 工具。

Agent 执行一条命令有时限（通常几分钟），而视频要等十几分钟。设 `WRALLY_MAX_WAIT`（比时限短一些），或者把提交和等待分开跑：一集里的生成命令（`video gen` 等）默认就只提交，之后反复跑 `wrally wait <kind> --timeout <秒>`；单独生成（`gen video` 等）默认会一直等到下载完，要加 `--no-wait` 只提交，之后反复跑 `wrally gen fetch -o <目录> --timeout <秒>`。直到不再显示 `running` 为止。命令被中途杀掉也不会弄坏状态，接着等即可，不要重新提交。

#### 在 WorkBuddy 里使用

WorkBuddy 桌面端在本机执行命令，skill 格式和上面相同（文件夹加 `SKILL.md`）。和其他 Agent 相比多两处要设：skill 的位置，和沙箱的可写目录。

1. **先在系统终端里装好并登录**（不要在 WorkBuddy 里登录：`wrally login` 要在本机开端口等浏览器回调，沙箱里多半不行）：
   ```bash
   uv tool install --python 3.11 --editable ".[pitch]"
   wrally login
   ```
2. **把 skill 复制到 WorkBuddy 的目录**（国内版 `~/.workbuddy/skills/`，海外版 `~/.workbuddy-ai/skills/`）：
   ```bash
   cp -R skills/wrally ~/.workbuddy/skills/wrally
   ```
   用符号链接挂进去时，WorkBuddy 的 Agent 没有列出这个 skill（原因没确认），所以建议复制。复制的副本不会跟着仓库更新，wrally 升级后要删掉重新复制。
3. **放开 `~/.config/wrally/` 的写权限**：在 WorkBuddy 的沙箱设置里把它加进额外可写目录（对应 `~/.workbuddy/settings.json` 的 `sandbox.extraAllowWrite`，改文件前先退出 WorkBuddy）。**这一步不能省**：wrally 每次刷新 token 都要写回这个目录里的 `credentials.json`，而服务端每次刷新都会换新的 refresh_token。写不进去的话，旧的已经作废、新的又没存下来，只能重新登录。
4. **工作区选创作项目目录**（有 `wrally.yaml` 的那个）。wrally 写清单和下载素材都在单集目录里，工作区内的读写不受沙箱限制。
5. **权限保持默认，不要开「完全访问」**。默认权限下每条命令都要你点确认；生成命令花额度，这道确认正好用得上。
6. **不要在 WorkBuddy 里接 WorkRally 的连接器**，免得 Agent 绕开 wrally 直接调 MCP。
7. **等待时长**：没查到 WorkBuddy 一条命令最多能跑多久。等视频时让 Agent 在命令前加 `WRALLY_MAX_WAIT=240`，超时就再等一次（一集里的镜头用 `wrally wait`，单独生成用 `wrally gen fetch`）。

设好后重启 WorkBuddy，在技能列表里能看到 `wrally` 就说明装上了。

### 2. 准备创作项目

每部片子是一个独立的目录，放在这个仓库之外：

```
<项目>/
  wrally.yaml      # 各集共用的设置：短番项目 ID、模型、音色、公共提示词、检查规则
  CLAUDE.md        # 项目说明文件（或 AGENTS.md 等）：这部片子的制作规则和合成方法，给 Agent 看
  ep1/
    episode.yaml   # 这一集的清单
    prompts/       # 每镜独有的提示词
    .wrally/       # 下载的素材、对比图、报告、export.json
```

怎么写 `wrally.yaml`、清单和合成脚本，见 `skills/wrally/references/project-setup.md`。也可以直接跟 Agent 说「用 wrally 开一部新片子」，它会读这份说明来搭目录。

### 3. 跟 Agent 说要做什么

在创作项目目录里开 Agent 会话，用自然语言提要求即可，例如：

- 「看看这集进度」→ `wrally status`
- 「开新的一集，12 镜」→ `wrally new … --dry-run`，确认后再建
- 「把提示词推上去」→ `wrally lint`、`wrally push --dry-run`、`wrally push`
- 「生成这集的关键帧」→ `wrally keyframe gen --dry-run`，确认后 `--wait --fetch`，再把对比图给你选
- 「第 5 镜重做视频」→ `wrally video gen --shots 05 --redo --dry-run`
- 「检查一下素材」→ `wrally check`，只报有警告的镜头
- 「用 WorkRally 生成一段配音」→ `wrally gen audio … --dry-run`

生成类命令（`keyframe gen`、`video gen`、`audio gen`、`gen`）消耗真实额度，`push` 会覆盖远端的提示词，`new` 会在远端建剧集和场次。使用说明要求 Agent 对这些操作先 `--dry-run`，把要做的事告诉你，得到确认后再执行。选图始终由你决定。

### 一集的流程

| 步骤 | 命令 |
| --- | --- |
| 建档 | `wrally new --shots 00-11,99 --name "EP5 …"`，或拉取已有剧集 `wrally pull --series <ID>` |
| 写提示词和台词 | 编辑 `prompts/*.md` 和清单里的 `narration`，然后 `wrally lint`、`wrally push` |
| 关键帧 | `wrally keyframe gen --wait --fetch`，看 `.wrally/sheets/` 的对比图后 `wrally keyframe pick 02 A 03 B …` |
| 视频 | `wrally video gen --wait --fetch` |
| 旁白 | `wrally audio gen --wait --fetch` |
| 检查 | `wrally check`，返修就改提示词或台词后用 `--shots` 重跑对应的 `gen` |
| 合成 | `wrally export`，运行这部片子自己的合成脚本，再 `wrally page` 更新进度网页 |

`--shots` 支持 `all`、`02,05`、`02-07`；所有命令都有 `--json`；不确定选项时用 `wrally <命令> --help`。

## 开发

```bash
uv sync                                    # 安装依赖
uv run pytest -q                           # 测试，全部离线，用 tests/conftest.py 里的 FakeWorkRally 模拟远端
uv run ruff check . && uv run ruff format .
```

代码结构和改代码时要守的约束见 `CLAUDE.md`（`AGENTS.md` 是指向它的链接）。
