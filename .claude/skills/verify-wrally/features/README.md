# wrally 验证地图

这个目录是验证 wrally 用户可见行为的依据。驱动之前先读这一篇，再按对应的功能文件操作。文中的预期输出是在夹具上真实跑出来的（提交 `63bf9d7`，2026-11-10）。

## 基线

- 仓库根目录 `uv sync` 过，`bash "$S/doctor.sh"` 退出码 0（`S` 是 `.claude/skills/verify-wrally/scripts`）。
- `RUN=$(bash "$S/fixture.sh")` 建好了夹具，`E="$RUN/project/ep1"` 是单集目录。
- 夹具刚建好时：`01` 已选 A，`02` 有两张候选未选，`03` 只有提示词；`.wrally/` 下还没有任何报告，也没有 `page.html`。

## 驱动的约定

- 每条命令都写成 `bash "$S/drive.sh" "$RUN" <记录名> <wrally 参数…>`，下文只写 wrally 参数那一段，比如 `status -C "$E"`。
- 命令照抄，选项和引号不要改。
- 每篇都从基线状态开始。上一篇改过清单（`keyframe pick`）就重建一个夹具，或者按那一篇的说明还原。
- 稳定的抓手是：摘要行的文字、表头、退出码、错误码（`error: <code>`）、写出的文件的路径和 JSON 字段。表格列宽不是。

## 证据和跳过

- 命令行的证据是 `drive.sh` 留下的记录：命令、输出、退出码。
- 命令写文件的，证据里要有那个文件的副本；改清单的，要有改动前后的 `episode.yaml` 和两者的差别。
- 页面的证据是 `page.html` 里 `<script id="data">` 的 JSON 加一张截图。
- 每份证据注明功能和入口。到不了的入口，写下试过的命令和缺的前提；不要把它算成从别的入口验证过。

## 功能文件的格式

每篇以 H1 标题和一段用户可见行为的描述开头，然后是四个 H2，顺序固定：`Sub-features`、`How to get to it (user POV)`、`Driving it with drive.sh`、`Gotchas`。驱动那一节以 `Preconditions:` 开头，每一条把用户动作、确切的命令和看得到的结果配在一起。地图里不写实现细节。

## 功能

- [进度和检查](./progress-and-checks.md)：`status`、`lint`、`check`，单集和项目目录两种入口，规则命中、报告文件、项目目录下的报错。
- [导出](./export.md)：`export` 写 `.wrally/export.json`，缺素材的标记、`extra` 原样带出、重跑不改动、`--shots`。
- [进度网页](./page.md)：`page` 和其他命令的自动重建，单集页面、项目总览页、摘要文件、截图。
- [选图](./keyframe-pick.md)：`keyframe sheet` 出对比图，`keyframe pick` 改清单，选错时什么都不改。
- [调远端的命令](./remote.md)：夹具上只能证明它们被凭证挡住；真实项目上的只读命令和 `--dry-run`。这一篇里的真实项目部分没有在生成这个 skill 时跑过。
