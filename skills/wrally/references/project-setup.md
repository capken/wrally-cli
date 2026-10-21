# 开一部新片子：项目目录、清单和合成

目录：[目录结构](#目录结构) · [项目文件 wrally.yaml](#项目文件-wrallyyaml) · [建第一集](#建第一集) · [清单 episode.yaml](#清单-episodeyaml) · [rules](#rules) · [合成：export.json](#合成exportjson) · [给项目写 CLAUDE.md](#给项目写-claudemd)

wrally 不绑定任何一部片子。一部片子是一个**创作项目目录**，放在 wrally-cli 仓库之外；模型、音色、画风、检查阈值、合成版式都写在这个目录里。本机现成的例子是《提灯人》：`/Users/allenzheng/codes/lantern-bearer`，可以照着它的 `wrally.yaml` 和 `CLAUDE.md` 写。

## 目录结构

```
<项目>/
  wrally.yaml        # 项目文件：各集共用的设置。wrally 只读它，不改它
  CLAUDE.md          # 这部片子的制作规则、模型、合成方法
  styles/  assets/   # 各集共用的公共文本、配乐等
  index.html         # 项目总览页，wrally 生成
  ep1/               # 一集一个子目录（名字随意，有 episode.yaml 就算一集）
    episode.yaml     # 清单
    prompts/         # 每镜独有的提示词：02.image.md、02.anim.md
    styles/ assets/  # 这一集自己的公共文本、手工素材
    page.html        # 进度网页，wrally 生成，不要手改
    .wrally/         # 下载物和运行产物：keyframes/ video/ audio/ sheets/ raw/ report.md export.json …
```

## 项目文件 wrally.yaml

各集相同的设置写在这里，单集的 `episode.yaml` 只写不同的部分。合并规则：映射逐键合并，列表和单个值整个替换，单集里写 `null` 表示这一集不要这一项。这里的路径相对本文件所在的目录，单集清单里的路径相对单集目录。

```yaml
project_id: <WorkRally 短番项目 ID>
title: <片名>                      # 总览页的标题

defaults:
  image: {model: <图片模型 ID>, resolution: 4, aspect: "16:9", count: 2}
  video: {model: <视频模型 ID>, resolution: 1, duration: 8, audio: false}
  audio: {model: <旁白模型 ID>, voice_asset: <音色的媒资 ID>, voice_name: <绑定用的音色名>}
  style_suffix: styles/look.md            # 关键帧公共风格后缀（可选）
  anim_prefix: styles/look_anim.md        # 动画公共前缀（可选）
  narration_template: styles/narration.md # 旁白模板（可选），里面用 {voice_name}、{narration}
  border: wave                            # 公共文本里 {border} 占位符的默认取值（可选）
  borders:
    wave: {image: <关键帧里的写法>, anim: <动画里的写法>}

rules:                                    # 见下文；不写就什么都不查
  narration_max: 30
```

- 模型 ID 和各档分辨率的取值要向用户确认，不要猜，也不要从别的项目照抄了事：用哪个模型是用户的决定，ID 还可能随平台变化。
- 三种公共文本都是可选的，没配的那一类就不拼接，文件里写完整提示词。
- 音色名必须和旁白模板里 `<{voice_name}>` 出现的名字一致，`audio gen` 靠它绑定音色。
- wrally 不认识的顶层小节（比如给合成脚本用的 `assemble:`）可以随便加，wrally 不解释，`export` 会原样带出。

## 建第一集

```
mkdir ep1 && cd ep1
wrally new --shots 00-11,99 --name "EP1 <标题>" --title "<标题>" --dry-run   # 看会建什么
wrally new --shots 00-11,99 --name "EP1 <标题>" --title "<标题>"             # 用户确认后
```

`--shots` 是要建的镜号（习惯上 00 片头、99 片尾）。`new` 在远端建剧集和场次、在本地建清单和带占位文本的提示词文件；占位文本没换掉时 `lint` 报错。也可以先手写清单条目（不写 `story_id`、`series_id`）和 `prompts/` 再跑 `new`：条目按镜号认领并补上 `story_id`，已写好的提示词不会被占位文本覆盖，建完后 `wrally push`。重跑 `new` 只补缺的场次。它超时后不要重发，先 `wrally pull` 看远端实际建出了什么。

远端已经有剧集和场次时不用 `new`，用 `wrally pull --series <剧集 ID>` 建档。

## 清单 episode.yaml

`pull`、`new` 和生成命令会维护它；手写的注释和键顺序会保留。平时要手改的只有这些：

| 键 | 含义 |
| --- | --- |
| 顶层 `title` | 这一集的标题 |
| 顶层 `output` | 成片文件，相对单集目录，默认 `<目录名>.mp4` |
| 顶层 `defaults`、`rules` | 这一集和项目文件不同的部分 |
| 顶层 `page: false` | 不要自动重建进度网页 |
| 每镜 `narration` | 旁白台词，也是字幕文本 |
| 每镜 `border` | 这一镜的边框，不写用默认值 |
| 每镜 `raw: [image, anim]` | 这几类不拼接公共文本，文件里是完整提示词 |
| 每镜 `skip: [keyframe]` | 这一镜永远不由 wrally 生成这类素材（`keyframe`、`video`、`audio`） |
| 每镜 `video_duration` | 只给这一镜换视频时长（秒） |
| 每镜 `video_resolution` | 只给这一镜换视频分辨率（Seedance-2.0 Mini：1 是 480p、3 是 720p） |
| 每镜 `keyframe.refs: ["04", "06"]` | 用这些镜头的定稿关键帧作参考图生成（画布图生图；这条路径没有真实验证过，用之前告诉用户） |

不要手改的：`story_id`、`series_id`，以及 `keyframe`、`video`、`audio` 小节里的 `candidates`、`asset_id`、`synced`、`status`、`made_from`、`results_before` 等。它们是 wrally 判断「输入变没变、任务提交没提交」的依据，改错了会重复提交或把旧结果当成新的。选图用 `wrally keyframe pick`，不要手写 `pick`。

要换单镜的视频时长就写 `video_duration`，不要改 `defaults.video.duration`：时长计入每镜的输入指纹，改默认值会让整集的视频都被判为要重做。

## rules

`check` 和 `lint` 的阈值与词表。**没有缺省值**：没写的一项就不查，写错的键直接报 `bad_rules`。

```yaml
rules:
  f0_max: 97            # 旁白音高中位数上限（Hz）。设了才量音高；audio gen --redo-failed 也用它
  slow_max: 1.25        # 旁白时长 ÷ 视频时长的上限（旁白比画面长，合成时画面就得放慢）
  narration_max: 30     # 每镜台词字数上限，不计标点
  words:                # lint 在每镜自己写的提示词里查这些词；kind 是 anim（默认）或 image
    light effect: {kind: anim, words: [光束, 体积光]}
```

这些数是每部片子自己的经验（画风、音色、单镜时长不同，数就不同），和用户一起定，不要照搬别的项目的。词表检查会跳过否定写法（「不出现光束」「不碰杯」不算命中）。

## 合成：export.json

wrally 不做合成，也不调用合成脚本。`wrally export` 写出 `.wrally/export.json`，由这部片子自己的脚本读：

```json
{
  "version": 1,
  "title": "…",
  "output": "ep1.mp4",
  "project_dir": "..",
  "shots": [
    {
      "key": "07",
      "video": ".wrally/video/07.mp4", "video_seconds": 8.096,
      "audio": ".wrally/audio/07.wav", "audio_seconds": 9.74,
      "narration": "…",
      "skip": [],
      "missing": [],
      "stale": [],
      "extra": {}
    }
  ],
  "extra": {}
}
```

- 路径相对单集目录；脚本最好先切到单集目录再读。
- `missing` 列出 `video`、`audio`、`narration` 里缺的；`stale` 是过期判断的原文。缺素材时 `export` 照常导出、退出码仍是 0，要不要紧由脚本决定。
- `extra`：清单顶层和每镜里 wrally 不认识的键，原样带出（顶层的是项目文件和单集清单合并后的值）。合成要的参数就放在这种键里。里面的相对路径 wrally 不解析：约定是先按单集目录找，找不到再按 `project_dir` 找。
- `output` 是成片该写到的位置。合成完跑一次 `wrally page`，进度网页才会带上成片。

《提灯人》的 `toolkit/tideng_assemble.py` 是一个读这份文件的完整例子（ffmpeg 拼接、字幕、片名、配乐）。它的版式和参数是那部片子的，新片子可以参考结构，不要原样套用。

## 给项目写 CLAUDE.md

新项目建好后给它写一份 `CLAUDE.md`，以后的会话靠它知道这部片子的规矩。至少写：

- 所有 WorkRally 操作都用 `wrally`，不直接调 WorkRally MCP 工具。
- 用户指定的模型、音色及其 ID（连同是哪天核对的）。
- 这部片子的制作规则，以及每条规则是踩了什么坑定下来的。
- 怎么合成：命令、依赖、成片怎么核对。
- 目录是否用 git 管理；不用的话写明里面的东西只有一份，删改前要看清。
