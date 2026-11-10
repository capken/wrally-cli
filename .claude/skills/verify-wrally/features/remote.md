# 调远端的命令

`login`、`pull`、`push`、`new`、`fetch`、`wait`、`verify-picks`、`keyframe gen`、`video gen`、`video upscale`、`audio gen`、`gen`、`smoke`、`models`、`projects`、`upload`、`shots rm/order` 都要连 WorkRally。夹具的 ID 是假的，在它上面只能证明一件事：没有凭证时这些命令干净地失败。真实行为要在一个真实的创作项目里验证，其中很多会消耗额度或改远端的数据。

生成这个 skill 时只跑过下面「被凭证挡住」那一条；真实项目上的几条是照 `CLAUDE.md` 和命令的帮助写的，没有跑过。第一次用到时按实际结果改这一篇。

## Sub-features

- `auth-blocked` 没有凭证：错误码 `not_logged_in`，退出码 3，不打追踪栈。
- `remote-readonly` 只读的远端命令：`projects`、`models`、`verify-picks`。
- `pull-idempotent` 在已建档的一集上重跑 `pull`，不产生改动。
- `dry-run` `push`、`new`、生成类命令的 `--dry-run`：列出会做什么，不提交。
- `submit` 真的提交生成任务。只在用户明确同意后做。

## How to get to it (user POV)

- 先 `wrally login`（浏览器授权，凭证在 `~/.config/wrally/`）。
- 在真实的单集目录里敲对应的命令，或者用 `-C`。作者本机用哪个创作项目做回归，记在 `CLAUDE.local.md`。

## Driving it with drive.sh

Preconditions:

- 「被凭证挡住」用夹具和 `drive.sh`。
- 其余几条不用 `drive.sh`（它把凭证目录换成了空的）：`bash "$S/doctor.sh" --remote` 退出码 0 之后，直接在真实的单集目录里跑 `uv run --project <仓库> wrally …`，把输出和退出码自己存进 `$RUN/evidence/`。
- 跑之前先看那个创作项目的 `git status`，记下来，结束后再看一次。

- **被凭证挡住。** `projects`。退出码 3；输出只有一行 `error: not_logged_in  (run: wrally login)`。
- **只读。** `wrally projects`：退出码 0，一行摘要加一张不超过 15 行的项目表。`wrally models`：列出可用的模型。输出里不应该有 URL。
- **pull 零改动。** 在一集已建档、最近没人动过的目录里 `wrally pull`。预期清单和 `prompts/` 没有改动（`git status` 前后一致）。出现意外的 `local` 时不要接着 `push`，先弄清原因。
- **演练。** `wrally push --dry-run`、`wrally keyframe gen --shots <镜号> --dry-run`、`wrally video gen --shots <镜号> --dry-run`、`wrally audio gen --shots <镜号> --dry-run`。预期：列出会提交哪些镜头，清单不变。
- **Proof.** 每条命令的输出和退出码；前后两次 `git status`；`--dry-run` 的还要有清单的校验和前后一致。

## Gotchas

- 生成类命令（`keyframe gen`、`video gen`、`audio gen`、`video upscale`、`gen`、`smoke`）消耗真实额度，`push` 覆盖远端的提示词。验证时只跑 `--dry-run`；要真的提交，先向用户确认。
- `--dry-run` 不等于不调远端：`video upscale --dry-run` 会调一次只读的 `canvas_upscale_config` 来校验模型和倍数。别的命令的 `--dry-run` 调不调，以观察为准：看 `.wrally/raw/` 里有没有新文件。
- `pull` 会写本地的清单、`prompts/` 和 `.wrally/raw/`，不要在别人正在改的目录里随手跑。
- `.wrally/raw/` 里有签名地址和提示词全文：只看结构，不要整段打印，也不要复制进证据目录。
- 输出、`--json` 和 `-v` 里出现 URL 或提示词全文，本身就是一个要报告的问题。
- 提交超时后服务端可能已经受理：清单会停在 `submitting`，再跑报 `pending_submit`。这时用 `wait`、`fetch` 接上，不要重新提交。
