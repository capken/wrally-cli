# 进度和检查

用户在单集目录里看这一集做到哪一步（`status`）、提示词和台词有没有已知会出问题的写法（`lint`）、素材本身有没有问题（`check`）。三条命令都不调远端。`lint` 和 `check` 另外把明细写成报告文件。

## Sub-features

- `status-episode` 单集进度表：每镜的选图、视频、旁白状态，没做完的排前面。
- `status-project` 在项目目录下：每集一行。
- `lint-words` 动画或图片提示词里出现词表里的词，报警告。
- `lint-narration` 台词超过字数上限、缺台词，报警告。
- `lint-report` 明细写进 `.wrally/lint.md`。
- `check-slow` 旁白比视频长，放慢倍数超过上限，报警告。
- `check-sheets` 每镜一张抽帧拼图 `.wrally/sheets/<镜号>.jpg`。
- `check-report` 明细写进 `.wrally/report.md`，终端只列有警告的镜头。
- `no-manifest` 在项目目录下跑单集命令：错误码 `no_manifest`，退出码 2。
- `json` 加 `--json`：标准输出是一行 JSON。

## How to get to it (user POV)

- 在单集目录里敲 `wrally status`、`wrally lint`、`wrally check`，或者在别处用 `-C <单集目录>`。
- 在项目目录里敲 `wrally status`，或者 `wrally status -C <项目目录>`。
- `lint`、`check` 用 `--shots 02` 或 `--shots 01-02` 只看几镜。

## Driving it with drive.sh

Preconditions:

- 基线状态的夹具，`E="$RUN/project/ep1"`。

- **单集进度。** `status -C "$E"`。退出码 0；摘要行是 `3 shots: keyframe 1 picked, video 2 done, audio 2 done`；表头 `shot keyframe video audio f0`；第一行是 `03`（全是 `-`），`01` 的 keyframe 是 `A/2`，`02` 是 `-/2`。
- **项目进度。** `status -C "$RUN/project"`。退出码 0；摘要行以 `1 episodes: 0 with a film` 开头；一行 `ep1  3  1  2  2  -`。
- **lint。** `lint -C "$E"`。退出码 0；摘要行 `linted 3 shots: 3 warnings, 0 errors  -> .wrally/lint.md`；三行：`02 warn anim: fine motion words 写字`、`02 warn narration: 15 chars > 12`、`03 warn narration: missing`。`$E/.wrally/lint.md` 存在，表里是同样三行。
- **lint 只看一镜。** `lint --shots 01 -C "$E"`。摘要行 `linted 1 shots: 0 warnings, 0 errors`，没有表。
- **check。** `check -C "$E"`。退出码 0；摘要行 `checked 3 shots: 2 sheets, 2 warnings  -> .wrally/report.md`；只列一行 `02`：视频 `4.0s`、旁白 `6.0s`、`x1.50`，警告 `slow x1.50 > 1.25; narration 15 chars > 12`。`$E/.wrally/sheets/01.jpg` 和 `02.jpg` 存在；`$E/.wrally/report.md` 的表有三镜，`01` 是 `x0.75`、没有警告。
- **项目目录下的报错。** `lint -C "$RUN/project"`。退出码 2；输出一行 `error: no_manifest  (this is a project directory: use -C <episode>)`。
- **JSON。** `status --json -C "$E"`。退出码 0；标准输出能被 `python3 -m json.tool` 解析。
- **Proof.** 把 `$E/.wrally/lint.md`、`$E/.wrally/report.md` 复制到 `$RUN/evidence/`，用 Read 看一张抽帧拼图确认不是空图。

## Gotchas

- 夹具的 `rules` 没有 `f0_max`、`channel_gap_max`、`noise_floor_max`，所以音高、声道差、底噪都不量，`f0` 一列是 `-`。要验证这三项，在 `$RUN/project/wrally.yaml` 的 `rules` 里加上再跑；夹具的旁白是 110 Hz 的正弦波、双声道相同。量音高还要装了 `librosa`。
- `check` 会把量到的值写回清单的 `audio` 小节，属于会改清单的命令；夹具默认的规则下它不写任何东西。
- 抽帧拼图只在视频比拼图新时重做。换了视频文件而修改时间没变，拼图不会更新。
- 警告不影响退出码。`lint` 只有出现 error 级别（缺文件、占位文本没改、拼不出提示词）才以 2 退出。
- `check`、`lint` 结束后会顺带重建 `page.html`，见[进度网页](./page.md)。
