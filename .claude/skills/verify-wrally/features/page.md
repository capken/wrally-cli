# 进度网页

`wrally page` 把一集的当前进展做成 `page.html`：每镜一张卡片，有选用的关键帧和候选、视频、旁白、三类提示词和每类素材的状态。有项目文件时顺带重建项目目录下的总览页 `index.html`。会改清单或下载素材的命令结束后也自动重建它。不调远端。

## Sub-features

- `page-episode` 单集页面 `<单集目录>/page.html`。
- `page-index` 项目总览页 `<项目目录>/index.html`，每集一张卡片。
- `page-summary` `.wrally/page.summary.json`：总览页要用的几个数。
- `page-auto` 其他命令（`check`、`export`、`keyframe pick` 等）结束后自动重建。
- `page-idempotent` 输入没变，重跑报 `unchanged`。
- `page-render` 浏览器里实际显示的样子。
- `page-final` 项目文件或清单的 `page: {video: …, cut: …}` 用点号路径指向合成脚本自己的记录：`video` 里的镜头放另做的视频、标「成片用另做的视频」；`cut` 里的镜头卡片变淡、标「不进成片」，不算进度，也不在「未完成」筛选里。

## How to get to it (user POV)

- 在单集目录里敲 `wrally page`（`--open` 用浏览器打开）。
- 在项目目录里敲 `wrally page`：重建各集页面和总览页。
- 什么都不用敲：跑完别的命令，开着的页面自己更新。
- 用浏览器打开 `page.html` 或 `index.html`。
- 在 `wrally.yaml` 或 `episode.yaml` 里写 `page: {video: <点号路径>, cut: <点号路径>}`，再敲 `wrally page`。

## Driving it with drive.sh

Preconditions:

- 基线状态的夹具，`E="$RUN/project/ep1"`，还没有跑过任何命令（`$E/page.html` 不存在）。

- **生成单集页面。** `page -C "$E"`。退出码 0；摘要行 `page: 3 shots  -> page.html  (updated)`。`$E/page.html`、`$E/.wrally/page.summary.json`、`$E/.wrally/page.version.js` 和 `$RUN/project/index.html` 都出现了。
- **看摘要文件。** 读 `$E/.wrally/page.summary.json`。`shots` 是 3，`stats` 是 `{"audio": [2, 3], "keyframe": [1, 3], "video": [2, 3]}`，`open` 是 2，`film` 是 `false`，`title` 是 `第一集`。
- **看页面数据。** `page.html` 里 `<script id="data" type="application/json">` 的 JSON：`shots` 有三项；`01` 的 `keyframe.pick` 是 `A`、三类素材的 `status.state` 都是 `done`；`02` 的 `keyframe.status.state` 是 `pending`；`03` 的三类都是 `none`；`prompts.anim.before` 是 `镜头固定。`。
- **重跑。** `page -C "$E"`。摘要行以 `(unchanged)` 结尾。
- **自动重建。** 记下 `shasum "$E/page.html"`，然后 `keyframe pick 02 B -C "$E"`。不跑 `page`，校验和已经变了；`page.summary.json` 里 `stats.keyframe` 变成 `[2, 3]`、`open` 变成 1。
- **项目目录入口。** `page -C "$RUN/project"`。退出码 0，摘要行以 `page:` 开头、单位是 `episodes`。
- **截图。** `bash "$S/browser.sh" shot "$RUN" "$E/page.html" page`，用 Read 看打印出的 `$RUN/evidence/page.png`。侧栏标题是 `第一集`，三行统计各是 `n / 3`；`01`、`02` 两张卡片各有一张纯色关键帧和一个视频播放器，状态标签是绿色的「完成」；`03` 是灰色的「未开始」和「这一镜还没有台词」。
- **Proof.** `page.summary.json` 的副本、截图、`page` 命令的记录。

下面几步（`page-final`）接着上面的状态做，`02` 已经选了 B。先把 `$RUN/project/wrally.yaml` 存一份到 `$RUN/evidence/wrally.before.yaml`。

- **做一条另做的视频。** `mkdir -p "$RUN/project/assets" && ffmpeg -hide_banner -loglevel error -y -f lavfi -i "testsrc2=duration=2:size=320x180:rate=24" -pix_fmt yuv420p "$RUN/project/assets/重做-02.mp4"`。
- **只在项目文件里指过去。** 在 `wrally.yaml` 末尾追加三行（前两行缩进两格，接在已有的 `assemble:` 小节里）：`  video: {"02": assets/重做-02.mp4, "01": assets/没有.mp4}`、`  cut: ["03"]`、`page: {video: assemble.video, cut: assemble.cut}`。存一份 `wrally.after.yaml`，和改动前的 diff 正好是这三行。
- **重建。** `page -C "$E"`。摘要行以 `(updated)` 结尾。`page.summary.json` 里 `shots` 是 2、`open` 是 1、`stats` 是 `{"audio": [2, 2], "keyframe": [2, 2], "video": [1, 2]}`。
- **看页面数据。** `<script id="data">` 的 JSON 里：`01` 的 `replaced` 是 `../assets/没有.mp4`、`video.status.state` 是 `missing`；`02` 的 `replaced` 是 `../assets/重做-02.mp4`、`video.status.state` 是 `done`、`video.src` 以 `../assets/%E9%87%8D%E5%81%9A-02.mp4?v=` 开头；`03` 的 `cut` 是 `true`、`replaced` 是 `null`。
- **看运行后的 DOM。** `bash "$S/browser.sh" dom "$RUN" "$E/page.html" page-final-dom`。打印出 `s01` 的 `data-open="1"`、`s02` 的 `"0"`、`s03` 的 `class="shot cut"` 和 `data-open="0"`。`s03` 是 `"1"` 就说明不进成片的镜头还会出现在「未完成」筛选里。
- **截图。** `bash "$S/browser.sh" shot "$RUN" "$E/page.html" page-final 1400,2600`。`01`、`02` 的台词上方各有一条「成片用另做的视频：…」，视频说明是「视频（成片用）」，`01` 的视频框里是「文件缺失」；`03` 有「不进成片」一条，其余部分变淡，侧栏的点是灰色，三行统计是 `n / 2`。
- **重跑。** `page -C "$E"`。以 `(unchanged)` 结尾。
- **单集写 null 不要这一项。** 存一份 `episode.yaml`，在它末尾追加一行 `assemble: null`，再 `page -C "$E"`。以 `(updated)` 结尾；三镜的 `cut` 都是 `false`、`replaced` 都是 `null`，`01`、`02` 的视频又是 `.wrally/video/` 下的那条；DOM 里 `s03` 回到 `data-open="1"`。
- **Proof.** 改动前后的 `wrally.yaml` 和 `episode.yaml`、两份 DOM、截图、每次 `page` 的记录。

## Gotchas

- 夹具上只要先跑过 `check`、`lint`、`export`、`keyframe pick` 中的任何一条，`page.html` 就已经被自动建好了，这时第一次 `wrally page` 报的是 `unchanged`。这是对的，不是没生效。
- 自动重建不往标准输出写东西，也不改原命令的退出码；建不出来只在标准错误里留一行 `warn: page not updated`。`drive.sh` 的记录里含标准错误，看得到。
- 页面上的时间（「更新于」）是浏览器按文件信息显示的，页面内容里没有时间戳；素材地址后面的 `?v=` 是文件修改时间，重建夹具后会变，不要拿整份 `page.html` 和上一次运行的逐字比较。
- 成片是合成脚本在 wrally 之外写的。在 `$E/ep1.mp4` 放一个文件后要再跑一次 `wrally page`，`film` 才变成 `true`。
- 「需在网页上改」「网页副本不同」的标识来自 `.wrally/web_copies.json`，只有调过远端才有，夹具里没有，`web_differs` 恒为 0。
- `page-final` 的两处坑（2026-11-14 修过）：`assemble` 只写在项目文件里时值曾被丢掉，表现是加了 `page` 小节后 `wrally page` 报 `(unchanged)`；`cut` 的镜头曾仍是 `data-open="1"`。夹具的 `assemble` 本来就只在项目文件里，正好覆盖前一个；后一个只有导出的 DOM 看得出来，`page.html` 源文件和单元测试都看不到。
- `page.html` 源文件里只有数据和脚本，卡片、`data-open` 是浏览器里画出来的，要用 `browser.sh dom` 看，不要 grep `page.html`。
- 无界面截图里，没有封面的视频框可能是一块黑，即使视频本身有画面；要确认是哪条视频，看页面数据里的 `video.src`。
- 截图是静态的：开着的页面自动刷新、`J`/`K` 跳镜、侧栏随滚动高亮、`--watch` 都要真实浏览器才能验证，无界面截图证明不了。
