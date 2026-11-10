# 进度网页

`wrally page` 把一集的当前进展做成 `page.html`：每镜一张卡片，有选用的关键帧和候选、视频、旁白、三类提示词和每类素材的状态。有项目文件时顺带重建项目目录下的总览页 `index.html`。会改清单或下载素材的命令结束后也自动重建它。不调远端。

## Sub-features

- `page-episode` 单集页面 `<单集目录>/page.html`。
- `page-index` 项目总览页 `<项目目录>/index.html`，每集一张卡片。
- `page-summary` `.wrally/page.summary.json`：总览页要用的几个数。
- `page-auto` 其他命令（`check`、`export`、`keyframe pick` 等）结束后自动重建。
- `page-idempotent` 输入没变，重跑报 `unchanged`。
- `page-render` 浏览器里实际显示的样子。

## How to get to it (user POV)

- 在单集目录里敲 `wrally page`（`--open` 用浏览器打开）。
- 在项目目录里敲 `wrally page`：重建各集页面和总览页。
- 什么都不用敲：跑完别的命令，开着的页面自己更新。
- 用浏览器打开 `page.html` 或 `index.html`。

## Driving it with drive.sh

Preconditions:

- 基线状态的夹具，`E="$RUN/project/ep1"`，还没有跑过任何命令（`$E/page.html` 不存在）。

- **生成单集页面。** `page -C "$E"`。退出码 0；摘要行 `page: 3 shots  -> page.html  (updated)`。`$E/page.html`、`$E/.wrally/page.summary.json`、`$E/.wrally/page.version.js` 和 `$RUN/project/index.html` 都出现了。
- **看摘要文件。** 读 `$E/.wrally/page.summary.json`。`shots` 是 3，`stats` 是 `{"audio": [2, 3], "keyframe": [1, 3], "video": [2, 3]}`，`open` 是 2，`film` 是 `false`，`title` 是 `第一集`。
- **看页面数据。** `page.html` 里 `<script id="data" type="application/json">` 的 JSON：`shots` 有三项；`01` 的 `keyframe.pick` 是 `A`、三类素材的 `status.state` 都是 `done`；`02` 的 `keyframe.status.state` 是 `pending`；`03` 的三类都是 `none`；`prompts.anim.before` 是 `镜头固定。`。
- **重跑。** `page -C "$E"`。摘要行以 `(unchanged)` 结尾。
- **自动重建。** 记下 `shasum "$E/page.html"`，然后 `keyframe pick 02 B -C "$E"`。不跑 `page`，校验和已经变了；`page.summary.json` 里 `stats.keyframe` 变成 `[2, 3]`、`open` 变成 1。
- **项目目录入口。** `page -C "$RUN/project"`。退出码 0，摘要行以 `page:` 开头、单位是 `episodes`。
- **截图。** 用 `SKILL.md` 里的无界面 Chrome 命令截 `$E/page.html`，存 `$RUN/evidence/page.png`，用 Read 看。侧栏标题是 `第一集`，三行统计各是 `n / 3`；`01`、`02` 两张卡片各有一张纯色关键帧和一个视频播放器，状态标签是绿色的「完成」；`03` 是灰色的「未开始」和「这一镜还没有台词」。
- **Proof.** `page.summary.json` 的副本、截图、`page` 命令的记录。

## Gotchas

- 夹具上只要先跑过 `check`、`lint`、`export`、`keyframe pick` 中的任何一条，`page.html` 就已经被自动建好了，这时第一次 `wrally page` 报的是 `unchanged`。这是对的，不是没生效。
- 自动重建不往标准输出写东西，也不改原命令的退出码；建不出来只在标准错误里留一行 `warn: page not updated`。`drive.sh` 的记录里含标准错误，看得到。
- 页面上的时间（「更新于」）是浏览器按文件信息显示的，页面内容里没有时间戳；素材地址后面的 `?v=` 是文件修改时间，重建夹具后会变，不要拿整份 `page.html` 和上一次运行的逐字比较。
- 成片是合成脚本在 wrally 之外写的。在 `$E/ep1.mp4` 放一个文件后要再跑一次 `wrally page`，`film` 才变成 `true`。
- 「需在网页上改」「网页副本不同」的标识来自 `.wrally/web_copies.json`，只有调过远端才有，夹具里没有，`web_differs` 恒为 0。
- 截图是静态的：开着的页面自动刷新、`J`/`K` 跳镜、侧栏随滚动高亮、`--watch` 都要真实浏览器才能验证，无界面截图证明不了。
