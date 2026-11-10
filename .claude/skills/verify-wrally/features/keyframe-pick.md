# 选图

每镜的关键帧有若干张候选（A、B、…）。`keyframe sheet` 把候选拼成一张对比图给人看，`keyframe pick` 把人选定的那张写进清单。两条都只读写本地。

## Sub-features

- `sheet` 每镜一张对比图 `.wrally/sheets/keyframes-<镜号>.jpg`。
- `pick-letter` 用候选序号选：`keyframe pick 02 B`。
- `pick-validate` 序号超出范围或镜号不存在：报错，清单一个字都不改。
- `pick-last` `--last` 选尾帧，`-` 取消尾帧。
- `pick-page` 选完后进度网页自动更新。

## How to get to it (user POV)

- `wrally keyframe sheet`，然后打开 `.wrally/sheets/keyframes-<镜号>.jpg`，或者在进度网页上展开「候选关键帧」。
- `wrally keyframe pick <镜号> <序号> [<镜号> <序号> …]`，都可以加 `-C <单集目录>`。

## Driving it with drive.sh

Preconditions:

- 基线状态的夹具，`E="$RUN/project/ep1"`。
- 先留一份清单：`cp "$E/episode.yaml" "$RUN/evidence/episode.before-pick.yaml"`。

- **出对比图。** `keyframe sheet -C "$E"`。退出码 0；摘要行 `keyframe sheets: 2 written  -> .wrally/sheets/`；`01` 一行是 `2  A  .wrally/sheets/keyframes-01.jpg`，`02` 的 pick 是 `-`，`03` 是 `0  -  -`。两张图存在；用 Read 看 `keyframes-02.jpg`，是蓝、橙两块并排，各有标签。
- **选一张不存在的。** `keyframe pick 02 C -C "$E"`。退出码 2；输出 `error: unknown_candidate  (02 C; have A-B)`。`diff "$E/episode.yaml" "$RUN/evidence/episode.before-pick.yaml"` 没有输出。
- **选 B。** `keyframe pick 02 B -C "$E"`。退出码 0；摘要行 `picked 1 keyframes  -> episode.yaml`；一行 `02  B  kf02b  -`。
- **看清单。** `diff "$RUN/evidence/episode.before-pick.yaml" "$E/episode.yaml"`：只多出 `02` 的 `keyframe` 下的两行，`asset_id: kf02b` 和 `pick: B`，注释和其余内容都没动。
- **从另一条命令看结果。** `status -C "$E"`。摘要行变成 `3 shots: keyframe 2 picked, …`，`02` 的 keyframe 是 `B/2`。
- **重选同一张。** 再跑一次 `keyframe pick 02 B -C "$E"`。退出码 0，`was` 一列是 `B`，清单不变。
- **Proof.** 两次 `pick` 的记录、改动前的清单和 `diff` 的输出、`status` 的记录。

## Gotchas

- `pick` 改了清单，之后的状态不再是基线：`status`、`page` 的预期数字跟着变。接着验证别的功能前重建夹具，或者 `cp "$RUN/evidence/episode.before-pick.yaml" "$E/episode.yaml"` 还原后跑一次 `page -C "$E"`。
- 一次给多对时是全有或全无：有一对不对，其余的也不写。验证这条要一对对的加一对错的一起给。
- 给已经有视频的镜头换选图，表里会多一句 `video was made with the previous pick`。夹具的 `01` 可以试：`keyframe pick 01 B`。
- 序号以外的值被当成 asset_id 直接写进清单，不校验它存不存在。
- `--last` 写的是 `keyframe.last` 和 `last_pick`，它让 `video gen` 改走首尾帧模式；那一步调远端，夹具验证不了。
- 对比图只拼得出已下载的候选（`.wrally/keyframes/<镜号>/<序号>.*`）。真实工作区里候选没下载时那一列是 `none (run: wrally fetch keyframe …)`。
