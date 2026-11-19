  // ---- 中栏：这一镜 -------------------------------------------------------------
  const detail = document.getElementById("detail");
  const info = document.getElementById("info");
  let frameBox = null;

  function keyframeView(shot) {
    const kf = shot.keyframe, items = candidates(shot);
    const zoom = label => () => openCand(shot, Math.max(0, items.findIndex(c => c.label === label)));
    if (isPair(shot) && kf.src) {
      const side = (cls, tag, label, src) => el("div", { class: "pair-item " + cls },
        src ? el("img", { src, alt: "", onload: orient, onclick: zoom(label) }) : el("div", { class: "missing", text: "没有下载" }),
        el("div", { class: "pair-cap" }, el("span", { class: "kf-tag " + cls, text: tag }), el("span", { text: label })));
      return [el("div", { class: "pairs" }, side("start", "起始", kf.pick || "选用", kf.src),
        el("div", { class: "pair-arrow", text: "→" }), side("end", "尾帧", kf.last, kf.last_src))];
    }
    const src = cover(shot);
    if (!src) return [el("div", { class: "frame-empty" }, el("span", { text: "还没有关键帧" }),
      el("span", { class: "sub", text: kf.pick || !kf.candidates.length ? kf.status.label : "有候选，还没选" }))];
    const picked = items.findIndex(c => c.src === src);
    return [el("img", { src, alt: "", onload: orient, onclick: () => openCand(shot, picked) }),
      !kf.src && el("span", { class: "glass media-mode", style: "top:40px", text: `推荐 ${kf.suggest}（未选）` })];
  }
  function videoView(shot) {
    if (!shot.video.src) return [el("div", { class: "frame-empty" }, el("span", { text: "视频" + shot.video.status.label }),
      el("span", { class: "sub", text: shot.video.status.note || "生成后可以在这里播放" }))];
    // 另做的视频不拿清单里的关键帧当封面，免得看起来像旧画面
    return [el("video", { src: shot.video.src, poster: shot.replaced ? null : shot.keyframe.src, controls: true, playsinline: true,
      preload: "metadata", onloadedmetadata: orient })];
  }
  function drawFrame(shot, autoplay) {
    const video = current.media === "video";
    const sw = el("div", { class: "glass media-switch" }, [["keyframe", isPair(shot) ? "首尾帧" : "关键帧"], ["video", "▶ 视频"]].map(([value, label]) =>
      el("button", { "aria-pressed": String(current.media === value), text: label, onclick: () => setMedia(value) })));
    const mode = el("span", { class: "glass media-mode", text: video && shot.replaced ? "成片用另做的视频" : modeName(shot) });
    put(frameBox, ...(video ? videoView(shot) : keyframeView(shot)).filter(Boolean), mode, sw,
      !video && shot.video.src && !isPair(shot)
        ? el("button", { class: "play-badge", "aria-label": "播放这一镜的视频", onclick: () => setMedia("video", true) }, icon("play")) : null);
    const player = frameBox.querySelector("video");
    if (autoplay && player) player.play().catch(() => {});
  }
  function setMedia(value, autoplay) {
    current.media = value;
    const shot = byKey.get(current.key);
    if (shot) drawFrame(shot, autoplay);
  }

  function spec(label, text, tone, sub) {
    return el("div", { class: "spec" }, el("span", { class: "cap", text: label }),
      el("b", { class: tone === "ok" ? null : tone, text }), el("span", { class: "s", text: sub || " ", title: sub || null }));
  }
  function specs(shot) {
    const kf = shot.keyframe, v = shot.video.status, a = shot.audio.status;
    const kfText = kf.pick && kf.last ? `${kf.pick} → ${kf.last}` : kf.pick ? "选用 " + kf.pick : kf.status.label;
    const kfSub = kf.candidates.length ? `共 ${kf.candidates.length} 张` + (kf.suggest && kf.suggest !== kf.pick ? ` · 推荐 ${kf.suggest}` : "") : kf.status.note;
    return el("div", { class: "specs" },
      spec("关键帧", kfText, TONE[kf.status.state], kfSub),
      spec("视频", v.label, TONE[v.state], v.note || (shot.replaced ? "成片用另做的视频" : modeName(shot))),
      spec("旁白", a.label, TONE[a.state], a.note || (shot.audio.f0 !== null ? `音高 ${shot.audio.f0} Hz` : "")));
  }
  function alerts(shot) {
    const out = [];
    if (shot.cut) out.push(el("div", { class: "alert" }, el("span", { class: "glyph", "data-g": "–" }),
      el("span", null, el("strong", { text: "不进成片" }), el("span", { class: "t", text: "：素材留着，合成时跳过这一镜。" }))));
    else if (shot.replaced) out.push(el("div", { class: "alert" }, el("span", { class: "glyph", "data-g": "i" }),
      el("span", null, el("strong", { text: "成片用另做的视频" }),
        el("span", { class: "t", text: `：${shot.replaced}。关键帧、候选和提示词是清单里的，不一定是这条视频用的。` }))));
    for (const [kind, label] of KINDS) {
      const st = shot[kind].status;
      if (BAD.includes(st.state)) out.push(el("div", { class: "alert " + (st.state === "failed" ? "bad" : "warn") },
        el("span", { class: "glyph", "data-g": "!" }),
        el("span", null, el("strong", { text: `${label}${st.label}` }), st.note && el("span", { class: "t", text: "：" + st.note }))));
    }
    for (const [kind, , label] of PROMPTS) {
      const p = shot.prompts[kind];
      if (p && p.web === "differs") out.push(el("button", { class: "alert warn", onclick: () => openPrompt(shot, kind) },
        el("span", { class: "glyph", "data-g": "!" }),
        el("span", null, el("strong", { text: `${label}的网页副本不同` }), el("span", { class: "t", text: "：生成用的是网页那份，push 改不到它。点这里看怎么改。" }))));
    }
    return out.length ? el("div", { class: "alerts" }, out) : null;
  }
  function strip(shot) {
    const kf = shot.keyframe, items = candidates(shot);
    const missing = kf.candidates.filter(c => !c.src);
    const pick = [kf.pick ? "选用 " + kf.pick : kf.src ? "已选用" : "还没选", kf.last && "尾帧 " + kf.last,
      kf.suggest && kf.suggest !== kf.pick && "推荐 " + kf.suggest].filter(Boolean).join(" · ");
    if (!items.length && !missing.length) return [el("div", { class: "cands-head" }, el("span", { class: "cap", text: "候选" })),
      el("div", { class: "empty-cands" }, el("span", { text: "还没有候选关键帧" }), el("span", { class: "sub", text: kf.status.label }))];
    return [el("div", { class: "cands-head" }, el("span", { class: "cap", text: `候选 ${kf.candidates.length || 1} 张` }),
      el("span", { class: "pick", title: kf.suggest_note || null, text: pick })),
    el("div", { class: "strip" }, items.map((c, i) => {
      const mark = c.picked ? "选用" : c.last ? "尾帧" : c.suggested ? "推荐" : "";
      return el("button", { class: "cand" + (c.picked ? " picked" : c.last ? " last" : c.suggested ? " suggested" : ""),
        title: candName(c), onclick: () => openCand(shot, i) },
        el("img", { src: c.src, alt: "", loading: "lazy" }), el("span", { class: "a", text: c.label }), mark && el("span", { class: "m", text: mark }));
    }), missing.map(c => el("div", { class: "cand", title: "没有下载" }, "未下载", el("span", { class: "a", text: c.label }))))];
  }
  function refChip(shot) {
    const refs = shot.keyframe.refs;
    if (!refs.length) return null;
    return el("button", { class: "ref-chip", title: refs.map(r => r.name).join(" / "), onclick: () => openRefs(shot) },
      el("span", { class: "ref-stack" }, refs.slice(0, 3).map(r => r.src ? el("img", { src: r.src, alt: "" }) : el("span"))),
      el("span", { text: `参考图 ${refs.length}` }));
  }
  function drawDetail(shot) {
    const [label, tone] = badge(shot);
    frameBox = el("div", { class: "frame" });
    detail.className = "detail" + (shot.cut ? " cut" : "");
    put(detail,
      el("div", { class: "dhead" },
        el("div", { class: "dhead-main" },
          el("div", { class: "cap" }, `镜头 ${shot.key} · `, el("span", { class: isPair(shot) ? "pair" : null, text: modeName(shot) }),
            ` · ${data.shots.indexOf(shot) + 1} / ${data.shots.length}`),
          el("h2", { class: "dtitle" + (shot.narration ? "" : " none"), title: shot.narration || null, text: shot.narration || "这一镜还没有台词" })),
        el("div", { class: "dhead-side" }, refChip(shot), el("span", { class: "badge " + tone, text: label }))),
      frameBox, specs(shot), alerts(shot), ...strip(shot));
    drawFrame(shot);
    const film = detail.querySelector(".strip");
    if (film) requestAnimationFrame(() => revealIn(film, film.querySelector(".picked")));
  }

  // ---- 右栏：提示词、台词与旁白、检查 ------------------------------------------
  function audioPlayer(shot) {
    const a = shot.audio;
    if (!a.src) return el("div", { class: "audio-empty", text: "旁白" + a.status.label + (a.status.note ? "：" + a.status.note : "") });
    const audio = el("audio", { src: a.src, preload: "metadata" });
    const button = el("button", { class: "audio-play", "aria-label": "播放 / 暂停旁白", onclick: () => audio.paused ? audio.play() : audio.pause() });
    const fill = el("i"), time = el("span", { class: "audio-time" });
    const track = el("div", { class: "audio-track", role: "slider", "aria-label": "旁白进度", onclick: e => {
      if (!audio.duration) return;
      const r = track.getBoundingClientRect();
      audio.currentTime = Math.min(1, Math.max(0, (e.clientX - r.left) / (r.width || 1))) * audio.duration;
    } }, el("span", null, fill));
    const sync = () => {
      const d = audio.duration || 0;
      fill.style.width = d ? audio.currentTime / d * 100 + "%" : "0";
      time.textContent = `${fmt(audio.currentTime)} / ${d ? fmt(d) : "–:––"}`;
      put(button, icon(audio.paused ? "play" : "pause"));
    };
    for (const name of ["timeupdate", "loadedmetadata", "play", "pause", "ended"]) audio.addEventListener(name, sync);
    sync();
    return el("div", { class: "audio" }, audio, button, track, time);
  }
  function checkRow(label, value, tone, note) {
    return el("div", { class: "check" }, el("span", { class: "k", text: label }), el("span", { class: "n", title: note || null, text: note || "" }),
      el("span", { class: "v " + tone, text: value }));
  }
  function measure(label, value, unit, limit) {
    if (value === null || value === undefined) return null;
    const over = limit !== null && limit !== undefined && value > limit;
    return checkRow(label, `${value} ${unit}`, limit === null || limit === undefined ? "" : over ? "warn" : "ok",
      limit === null || limit === undefined ? "" : `上限 ${limit} ${unit}`);
  }
  function drawInfo(shot) {
    const limits = data.limits || {};
    const webs = PROMPTS.filter(([k]) => shot.prompts[k] && shot.prompts[k].web);
    const differs = webs.filter(([k]) => shot.prompts[k].web === "differs");
    put(info,
      el("section", { class: "block" }, el("div", { class: "cap", text: "提示词" }),
        el("div", { class: "prompts" }, PROMPTS.map(([kind, short]) => {
          const p = shot.prompts[kind];
          return el("button", { class: "prow", "aria-label": `查看${short}提示词全文`, onclick: () => openPrompt(shot, kind) },
            el("span", { class: "k" }, short, p && p.web && el("span", { class: "web-dot " + p.web })),
            el("span", { class: "txt" + (p ? "" : " muted"), text: p ? (p.own || p.before + p.after) : "没有" }),
            el("span", { class: "more", text: "全文" }));
        }))),
      el("section", { class: "block" }, el("div", { class: "cap", text: "台词 / 旁白" }),
        el("p", { class: "quote" + (shot.narration ? "" : " none"), text: shot.narration || "这一镜还没有台词" }),
        audioPlayer(shot)),
      el("section", { class: "block" }, el("div", { class: "cap", text: "检查" }),
        el("div", { class: "checks" },
          KINDS.map(([kind, label]) => checkRow(label, shot[kind].status.label, TONE[shot[kind].status.state], shot[kind].status.note)),
          measure("音高", shot.audio.f0, "Hz", limits.f0_max),
          measure("声道差", shot.audio.channel_gap, "dB", limits.channel_gap_max),
          measure("底噪", shot.audio.noise_floor, "dB", limits.noise_floor_max),
          webs.length ? checkRow("网页副本", differs.length ? "不同" : "一致", differs.length ? "warn" : "ok",
            (differs.length ? differs : webs).map(([, short]) => short).join("、")) : null)),
      el("div", { class: "info-foot", text: "只读：选图用 wrally keyframe pick，改提示词后 wrally push" }));
  }

  function focusShot(key) {
    const shot = byKey.get(key);
    if (!shot) return;
    if (key !== current.key) current.cand = null;
    current.key = key;
    for (const b of rail.querySelectorAll(".shot")) b.setAttribute("aria-current", String(b.dataset.shot === key));
    revealIn(rail, rail.querySelector('[aria-current="true"]'));
    if (current.media === "video" && !shot.video.src) current.media = "keyframe";
    drawDetail(shot);
    drawInfo(shot);
    // 相邻两镜的图先在后台载入，换镜时不用等
    const list = shown(), at = list.indexOf(shot);
    for (const near of [list[at - 1], list[at + 1]]) {
      if (near) for (const c of near.keyframe.candidates) if (c.src) new Image().src = c.src;
    }
  }
  function nothing() {
    current.key = null;
    put(detail, el("div", { class: "frame-empty" }, el("span", { text: "这一集还没有镜头" })));
    put(info);
  }
