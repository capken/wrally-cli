# wrally

`wrally` 是一个本地命令行工具，用于在 [WorkRally](https://workrally.qq.com) 上批量制作单集短视频（层级为短番项目 → 剧集 → 场次）。它以脚本方式完成关键帧、视频、旁白的生成与检查，并导出合成所需的输入。

`wrally` 的主要使用者是能够执行终端命令的 AI Agent，例如 Claude Code、Codex CLI、Gemini CLI、Cursor、WorkBuddy，不依赖特定厂商。用户也可以在终端中直接使用。

## 设计目标

由 Agent 逐镜调用 WorkRally MCP 工具制作一集十余镜的短视频时，存在以下三个问题：

| 问题 | 现象 | wrally 的处理方式 |
| --- | --- | --- |
| 上下文膨胀 | 返回内容包含签名下载地址和提示词全文，且无法按字段筛选。一集 14 镜的场次列表约 10 万字符，其中签名地址约 4 万字符；单镜视频结果中一条地址约 1.2k 字符；单图视频的模型列表约 2.8 万字符；54 个工具的定义合计约 9 万字符。会话后期旧内容被清理，已完成的步骤需要重做 | 一条命令处理整集，终端只输出一行结论和一张不超过 30 行的表格；地址、提示词全文和原始返回只写入本地文件 |
| 调用次数过多 | 查询结果每次只能查一镜，且关键帧、视频、旁白需分别查询，14 镜轮询一轮即 14 次调用；绑定音色每次一镜；单图视频每镜的参考图不同，配置需逐镜写入。一轮操作需数十次工具调用 | 在工具内部批量、并发调用；一集从关键帧到成片，Agent 侧约需十余次 bash 调用 |
| 流程约束依赖人工记忆 | 调用顺序错误导致「未配置模型」；未写入单图配置即生成视频，画面不参考关键帧；查询单个场次的接口返回空提示词；网页上编辑过的镜头另存一份提示词副本，接口无法修改；选图时镜号记错 | 调用顺序、公共提示词拼接、单图配置、网页副本检查、选图核对、素材过期检查均作为默认行为内置 |

以上数据于 2026-10-31 对 WorkRally MCP 实测得出（测量对象为工具定义，以及一集 14 镜的只读调用）。WorkRally 的工具说明已加入部分提示（如生成视频前需确认、建议传入剧集 ID），但上述三个问题仍然存在。

此外，以下设计使 `wrally` 适合被 Agent 反复调用：

- **文件即状态**：一集的全部状态保存在 `episode.yaml`（清单）和 `prompts/` 中，不依赖对话记忆，新会话可直接继续。
- **幂等**：输入未变化时不重复提交，已下载的文件不重复下载。命令中途失败后，重新运行同一条命令即可从中断处继续。
- **退出码**：

  | 退出码 | 含义 |
  | --- | --- |
  | `0` | 全部成功 |
  | `1` | 部分失败或等待超时 |
  | `2` | 前置检查未通过，未提交任何任务 |
  | `3` | 鉴权或网络错误 |
  | `4` | wrally 内部的意外错误。终端不输出追踪栈，追踪栈写入凭证目录下的 `last-crash.txt` |

- **机器可读输出**：所有命令支持 `--json`。成功时输出一行 `{"summary": …, "rows": [...]}`，失败时输出一行 `{"error": …, "hint": …, "exit_code": …}`。

`wrally` 不提供以下功能：

- 选图。选图由用户决定。
- 剪辑合成。`wrally export` 只生成一份中性的 `.wrally/export.json`，由各部片子自己的合成脚本读取。
- 片子专属的设置。画风、字体、阈值和词表写在各自的创作项目目录中。

如果只需生成一张图片、一段视频、一段配音或一段音乐，且不属于任何一集，使用 `wrally gen`，无需创建项目。

## 安装

### 前提条件

- [uv](https://docs.astral.sh/uv/)（自动安装 Python 3.11）
- `ffmpeg` 和 `ffprobe`，用于抽帧以及测量时长和音量。macOS 上运行 `brew install ffmpeg` 安装
- 可访问 WorkRally 的账号

### 安装步骤

```bash
git clone git@github.com:capken/wrally-cli.git
cd wrally-cli

# 安装为全局命令 wrally。--editable 使源码修改立即生效；
# [pitch] 安装可选依赖 librosa，用于测量旁白音高
uv tool install --python 3.11 --editable ".[pitch]"

# 通过浏览器授权，凭证保存在 ~/.config/wrally/credentials.json
wrally login
```

如果不安装全局命令，可在仓库目录中运行 `uv sync`，然后通过 `uv run wrally …` 调用。无法连接 PyPI 时，为 `uv` 添加 `--default-index <镜像地址>`。

### 登录说明

`wrally login` 会打开浏览器，授权完成后回调本机 `127.0.0.1` 上的一个端口，因此必须在有浏览器的计算机上运行。

运行在云端沙箱中的 Agent（如 Codex Cloud、远程容器）无法接收该回调，目前没有专门的支持。替代方法是在本机登录后，将 `credentials.json` 复制到沙箱中，并用 `WRALLY_CONFIG_DIR` 指向其所在目录。

> **注意**：每次刷新 token 时服务端都会签发新的 refresh_token。同一份凭证不能在两处同时使用，否则先刷新的一方会使另一方的凭证失效，失效的一方只能重新登录。

### 环境变量

以下环境变量均为可选：

| 变量 | 说明 |
| --- | --- |
| `WRALLY_CONFIG_DIR` | 凭证目录。默认为 `~/.config/wrally/` |
| `WRALLY_MCP_URL` | WorkRally MCP 地址。默认为 `https://workrally.qq.com/zenstudio/api/mcp` |
| `WRALLY_CONCURRENCY` | 查询和下载的并发数。默认为 4 |
| `WRALLY_PROJECT` | `wrally gen` 使用的默认短番项目 ID |
| `WRALLY_MAX_WAIT` | 所有等待操作的上限（秒），优先于各命令的 `--timeout`。如果 Agent 对单条命令有执行时限，将其设为略小于该时限的值 |

## 在 Agent 中使用

### 步骤 1：接入使用说明

使用说明位于 `skills/wrally/SKILL.md`，详细内容位于 `skills/wrally/references/project-setup.md`。说明涵盖可用命令、一集的标准流程、需事先确认的计费操作，以及各类错误的处理方式。说明内容不依赖特定 Agent，按所用 Agent 的方式接入：

| Agent | 接入方式 |
| --- | --- |
| Claude Code | 以 skill 方式挂载：`ln -s "$(pwd)/skills/wrally" ~/.claude/skills/wrally`。挂载后，在任意目录中，当任务涉及 WorkRally 的剧集、场次、提示词、关键帧、视频、旁白，或目录中存在 `episode.yaml`、`wrally.yaml` 时，Claude Code 会自动加载该 skill |
| Codex CLI 及其他读取 `AGENTS.md` 的 Agent | 在创作项目的 `AGENTS.md`（或全局的 `~/.codex/AGENTS.md`）中加入：「WorkRally 的操作一律使用 `wrally` 命令，操作前先阅读 `<wrally-cli 路径>/skills/wrally/SKILL.md`」 |
| WorkBuddy | 复制为 skill，并额外开放一个沙箱可写目录。见[在 WorkBuddy 中使用](#在-workbuddy-中使用) |
| Gemini CLI、Cursor 等 | 写入其读取的说明文件（如 `GEMINI.md`、`.cursor/rules/`），或将 `SKILL.md` 的正文直接放入其中 |

无论使用哪种 Agent，原则相同：只通过 bash 调用 `wrally`，根据退出码和摘要输出判断结果，不直接调用 WorkRally 的 MCP 工具。

#### 处理命令执行时限

Agent 对单条命令通常有几分钟的执行时限，而视频生成可能需要十几分钟。可采用以下任一方式：

- 设置 `WRALLY_MAX_WAIT`，取值略小于 Agent 的执行时限。
- 将提交与等待分开执行：
  - 单集中的生成命令（如 `video gen`）默认只提交。提交后反复运行 `wrally wait <kind> --timeout <秒>`。
  - 单独生成命令（如 `gen video`）默认会等待到下载完成。添加 `--no-wait` 只提交，之后反复运行 `wrally gen fetch -o <目录> --timeout <秒>`。

  重复运行，直到输出中不再出现 `running`。

命令被中途终止不会损坏状态，继续等待即可，不要重新提交。

#### 在 WorkBuddy 中使用

WorkBuddy 桌面端在本机执行命令，skill 格式与 Claude Code 相同（文件夹加 `SKILL.md`）。与其他 Agent 相比，需要额外配置 skill 的位置和沙箱的可写目录。

1. 在系统终端中安装并登录。不要在 WorkBuddy 中登录：`wrally login` 需要在本机监听端口以接收浏览器回调，在沙箱中通常无法完成。

   ```bash
   uv tool install --python 3.11 --editable ".[pitch]"
   wrally login
   ```

2. 将 skill 复制到 WorkBuddy 的 skill 目录。国内版为 `~/.workbuddy/skills/`，海外版为 `~/.workbuddy-ai/skills/`。

   ```bash
   cp -R skills/wrally ~/.workbuddy/skills/wrally
   ```

   使用符号链接挂载时，WorkBuddy 的 Agent 未能识别该 skill（原因未确认），因此建议复制。复制的副本不会随仓库更新，升级 wrally 后需删除并重新复制。

3. 开放 `~/.config/wrally/` 的写权限：在 WorkBuddy 的沙箱设置中将该目录加入额外可写目录，对应 `~/.workbuddy/settings.json` 中的 `sandbox.extraAllowWrite`。修改该文件前先退出 WorkBuddy。

   > **重要**：此步骤不可省略。wrally 每次刷新 token 时都要将新凭证写回该目录下的 `credentials.json`，而服务端每次刷新都会签发新的 refresh_token。如果写入失败，旧凭证已作废而新凭证未保存，只能重新登录。

4. 将工作区设为创作项目目录（包含 `wrally.yaml` 的目录）。wrally 写入清单和下载素材都在单集目录中进行，工作区内的读写不受沙箱限制。

5. 保持默认权限，不要启用「完全访问」。默认权限下每条命令都需要用户确认，可用于把关消耗额度的生成命令。

6. 不要在 WorkBuddy 中接入 WorkRally 连接器，以免 Agent 绕过 wrally 直接调用 MCP。

7. 设置等待时长。WorkBuddy 单条命令的最长执行时间尚未确认。等待视频时，让 Agent 在命令前加上 `WRALLY_MAX_WAIT=240`，超时后再次等待：单集镜头使用 `wrally wait`，单独生成使用 `wrally gen fetch`。

完成配置后重启 WorkBuddy。技能列表中出现 `wrally` 即表示安装成功。

### 步骤 2：准备创作项目

每部片子使用一个独立目录，位于本仓库之外：

```
<项目>/
  wrally.yaml      # 各集共用的设置：短番项目 ID、模型、音色、公共提示词、检查规则
  CLAUDE.md        # 项目说明文件（或 AGENTS.md 等），供 Agent 阅读：本片的制作规则和合成方法
  ep1/
    episode.yaml   # 本集清单
    prompts/       # 每镜独有的提示词
    .wrally/       # 下载的素材、对比图、报告、export.json
```

`wrally.yaml`、清单和合成脚本的写法见 `skills/wrally/references/project-setup.md`。也可以要求 Agent「用 wrally 开一部新片子」，Agent 会依据该说明创建目录。

### 步骤 3：向 Agent 下达任务

在创作项目目录中启动 Agent 会话，用自然语言描述任务。请求与对应命令示例如下：

| 请求 | Agent 执行的命令 |
| --- | --- |
| 「看看这集进度」 | `wrally status` |
| 「开新的一集，12 镜」 | `wrally new … --dry-run`，确认后正式创建 |
| 「把提示词推上去」 | `wrally lint`、`wrally push --dry-run`、`wrally push` |
| 「生成这集的关键帧」 | `wrally keyframe gen --dry-run`，确认后加 `--wait --fetch` 执行，再提供对比图供用户选图 |
| 「第 5 镜重做视频」 | `wrally video gen --shots 05 --redo --dry-run` |
| 「检查一下素材」 | `wrally check`，只报告有警告的镜头 |
| 「用 WorkRally 生成一段配音」 | `wrally gen audio … --dry-run` |
| 「测一下生成接口能不能用」 | `wrally smoke --dry-run`，确认后运行 `wrally smoke`，依次生成图像、配音和 5 秒视频并检查文件 |

以下操作会产生实际影响：

- 生成类命令（`keyframe gen`、`video gen`、`audio gen`、`gen`、`smoke`）消耗真实额度。
- `push` 覆盖远端的提示词。
- `new` 在远端创建剧集和场次。

使用说明要求 Agent 对这些操作先运行 `--dry-run`，向用户说明将要执行的操作，经确认后再执行。选图始终由用户决定。

### 单集流程

| 步骤 | 命令 |
| --- | --- |
| 建档 | `wrally new --shots 00-11,99 --name "EP5 …"`；或拉取已有剧集：`wrally pull --series <ID>` |
| 编写提示词和台词 | 编辑 `prompts/*.md` 和清单中的 `narration`，然后运行 `wrally lint`、`wrally push` |
| 关键帧 | `wrally keyframe gen --wait --fetch`；查看 `.wrally/sheets/` 中的对比图后运行 `wrally keyframe pick 02 A 03 B …` |
| 视频 | `wrally video gen --wait --fetch` |
| 旁白 | `wrally audio gen --wait --fetch` |
| 检查 | `wrally check`。需要返修时，修改提示词或台词，再用 `--shots` 重新运行对应的生成命令 |
| 合成 | `wrally export`，运行本片的合成脚本，然后运行 `wrally page` 更新进度网页 |

`--shots` 支持 `all`、`02,05`、`02-07` 三种写法。所有命令均支持 `--json`。查看命令选项使用 `wrally <命令> --help`。

## 开发

```bash
uv sync                                    # 安装依赖
uv run pytest -q                           # 运行测试。测试全部离线，使用 tests/conftest.py 中的 FakeWorkRally 模拟远端
uv run ruff check . && uv run ruff format .  # lint 与格式化
```

代码结构和修改代码时需遵守的约束见 `CLAUDE.md`（`AGENTS.md` 是指向它的符号链接）。
