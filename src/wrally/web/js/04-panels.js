  // ---- 中栏：这一镜 -------------------------------------------------------------
  const detail = document.getElementById("detail");
  const info = document.getElementById("info");
  const drawerBackdrop = document.getElementById("drawer-backdrop");
  let frameBox = null;

  function setDrawer(open) {
    document.body.classList.toggle("drawer-open", Boolean(open));
    const toggle = detail.querySelector(".info-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", String(Boolean(open)));
  }
  function toggleDrawer() {
    setDrawer(!document.body.classList.contains("drawer-open"));
  }
  if (drawerBackdrop) drawerBackdrop.addEventListener("click", () => setDrawer(false));

  function hasPlayableVideo(shot) {
    const up = upscaleOf(shot);
    return Boolean(shot.video.src || (up && up.status && up.status.state === "done" && up.src));
  }

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
    const up = upscaleOf(shot);
    const hasFreshUpscale = Boolean(up && up.status && up.status.state === "done" && up.src);
    if (!shot.video.src && !hasFreshUpscale) {
      return [el("div", { class: "frame-empty" }, el("span", { text: "视频" + shot.video.status.label }),
        el("span", { class: "sub", text: fmtNote(shot.video.status.note) || "生成后可以在这里播放" }))];
    }
    const useUpscaled = hasFreshUpscale && (current.videoSource !== "original" || !shot.video.src);
    const videoSrc = useUpscaled ? up.src : shot.video.src;
    const upLabel = up && up.scale ? `超分 x${up.scale}` : "超分版";
    const toggle = hasFreshUpscale && shot.video.src ? el("div", { class: "glass upscale-switch", role: "group", "aria-label": "切换视频版本" },
      el("button", { "aria-pressed": String(useUpscaled), text: upLabel, onclick: () => { current.videoSource = "upscaled"; drawFrame(shot); } }),
      el("button", { "aria-pressed": String(!useUpscaled), text: "原视频", onclick: () => { current.videoSource = "original"; drawFrame(shot); } })) : null;
    // 另做的视频不拿清单里的关键帧当封面，免得看起来像旧画面
    return [el("video", { src: videoSrc, poster: shot.replaced ? null : shot.keyframe.src, controls: true, playsinline: true,
      preload: "metadata", onloadedmetadata: orient }), toggle];
  }
  function drawFrame(shot, autoplay) {
    const video = current.media === "video";
    const sw = el("div", { class: "glass media-switch" }, [["keyframe", isPair(shot) ? "首尾帧" : "关键帧"], ["video", "▶ 视频"]].map(([value, label]) =>
      el("button", { "aria-pressed": String(current.media === value), text: label, onclick: () => setMedia(value) })));
    const mode = el("span", { class: "glass media-mode", text: video && shot.replaced ? "成片用另做的视频" : modeName(shot) });
    put(frameBox, ...(video ? videoView(shot) : keyframeView(shot)).filter(Boolean), mode, sw,
      !video && hasPlayableVideo(shot) && !isPair(shot)
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
    const kf = shot.keyframe, v = shot.video.status, a = shot.audio.status, up = upscaleOf(shot);
    const kfText = kf.pick && kf.last ? `${kf.pick} → ${kf.last}` : kf.pick ? "选用 " + kf.pick : kf.status.label;
    const kfSub = kf.candidates.length ? `共 ${kf.candidates.length} 张` + (kf.suggest && kf.suggest !== kf.pick ? ` · 推荐 ${kf.suggest}` : "") : fmtNote(kf.status.note);
    const vidParts = [v.note ? fmtNote(v.note) : (shot.replaced ? "成片用另做的视频" : modeName(shot))];
    if (shot.video.duration !== null && shot.video.duration !== undefined) vidParts.push(`${shot.video.duration}s`);
    if (shot.video.resolution !== null && shot.video.resolution !== undefined) vidParts.push(`分辨率 ${shot.video.resolution}`);
    if (up) vidParts.push(up.scale ? `超分 x${up.scale}` : "超分");
    return el("div", { class: "specs" },
      spec("关键帧", kfText, TONE[kf.status.state], kfSub),
      spec("视频", v.label, TONE[v.state], vidParts.filter(Boolean).join(" · ")),
      spec("旁白", a.label, TONE[a.state], fmtNote(a.note) || (shot.audio.f0 !== null && shot.audio.f0 !== undefined ? `音高 ${shot.audio.f0} Hz` : "")));
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
      if (BAD.includes(st.state)) {
        const reason = fmtNote(st.note);
        out.push(el("div", { class: "alert " + (st.state === "failed" ? "bad" : "warn") },
          el("span", { class: "glyph", "data-g": "!" }),
          el("span", null, el("strong", { text: `${label}${st.label}` }), reason && el("span", { class: "t", text: "：" + reason }))));
      }
    }
    const up = upscaleOf(shot);
    if (up && up.status && BAD.includes(up.status.state)) {
      const reason = fmtNote(up.status.note);
      out.push(el("div", { class: "alert " + (up.status.state === "failed" ? "bad" : "warn") },
        el("span", { class: "glyph", "data-g": "!" }),
        el("span", null, el("strong", { text: `视频超分${up.scale ? " x" + up.scale + " " : ""}${up.status.label}` }),
          reason && el("span", { class: "t", text: "：" + reason }))));
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
    const drawerOpen = document.body.classList.contains("drawer-open");
    frameBox = el("div", { class: "frame" });
    detail.className = "detail" + (shot.cut ? " cut" : "");
    put(detail,
      el("div", { class: "dhead" },
        el("div", { class: "dhead-main" },
          el("div", { class: "cap" }, `镜头 ${shot.key} · `, el("span", { class: isPair(shot) ? "pair" : null, text: modeName(shot) }),
            ` · ${data.shots.indexOf(shot) + 1} / ${data.shots.length}`),
          el("h2", { class: "dtitle" + (shot.narration ? "" : " none"), title: shot.narration || null, text: shot.narration || "这一镜还没有台词" })),
        el("div", { class: "dhead-side" },
          refChip(shot),
          el("button", { class: "info-toggle", "aria-expanded": String(drawerOpen), "aria-controls": "info",
            title: "展开或收起提示词与检查（快捷键 I）", text: "提示词与检查", onclick: toggleDrawer }),
          el("span", { class: "badge " + tone, text: label }))),
      frameBox, specs(shot), alerts(shot), ...strip(shot));
    drawFrame(shot);
    const film = detail.querySelector(".strip");
    if (film) requestAnimationFrame(() => revealIn(film, film.querySelector(".picked")));
  }

  // ---- 右栏：提示词、台词与旁白、检查 ------------------------------------------
  function audioPlayer(shot) {
    const a = shot.audio;
    if (!a.src) {
      const note = fmtNote(a.status.note);
      return el("div", { class: "audio-empty", text: "旁白" + a.status.label + (note ? "：" + note : "") });
    }
    const audio = el("audio", { src: a.src, preload: "metadata" });
    const button = el("button", { class: "audio-play", "aria-label": "播放 / 暂停旁白", onclick: () => audio.paused ? audio.play().catch(() => {}) : audio.pause() });
    const fill = el("i"), time = el("span", { class: "audio-time" });
    const track = el("div", {
      class: "audio-track", role: "slider", tabindex: "0", "aria-label": "旁白进度",
      "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0",
    }, el("span", null, fill));
    const seekAt = clientX => {
      if (!audio.duration || !isFinite(audio.duration)) return;
      const r = track.getBoundingClientRect();
      audio.currentTime = Math.min(1, Math.max(0, (clientX - r.left) / (r.width || 1))) * audio.duration;
    };
    let dragging = false;
    track.addEventListener("pointerdown", e => {
      if (e.button !== 0 && e.pointerType === "mouse") return;
      dragging = true;
      if (track.setPointerCapture) { try { track.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ } }
      seekAt(e.clientX);
    });
    track.addEventListener("pointermove", e => { if (dragging) seekAt(e.clientX); });
    const stopDrag = e => {
      if (!dragging) return;
      dragging = false;
      if (track.releasePointerCapture) { try { track.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ } }
    };
    track.addEventListener("pointerup", stopDrag);
    track.addEventListener("pointercancel", stopDrag);
    track.addEventListener("keydown", e => {
      if (!audio.duration || !isFinite(audio.duration)) return;
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        e.stopPropagation();
        audio.currentTime = Math.min(audio.duration, Math.max(0, audio.currentTime + (e.key === "ArrowRight" ? 2 : -2)));
      } else if (e.key === "Home") {
        e.preventDefault();
        audio.currentTime = 0;
      } else if (e.key === "End") {
        e.preventDefault();
        audio.currentTime = audio.duration;
      }
    });
    const sync = () => {
      const d = audio.duration || 0;
      const pct = d ? Math.round(audio.currentTime / d * 100) : 0;
      fill.style.width = d ? (audio.currentTime / d * 100) + "%" : "0";
      track.setAttribute("aria-valuenow", String(pct));
      time.textContent = `${fmt(audio.currentTime)} / ${d ? fmt(d) : "–:––"}`;
      put(button, icon(audio.paused ? "play" : "pause"));
    };
    for (const name of ["timeupdate", "loadedmetadata", "play", "pause", "ended"]) audio.addEventListener(name, sync);
    sync();
    return el("div", { class: "audio" }, audio, button, track, time);
  }
  function checkRow(label, value, tone, note) {
    return el("div", { class: "check" }, el("span", { class: "k", text: label }), el("span", { class: "n", title: note || null, text: note || "" }),
      el("span", { class: "v" + (tone ? " " + tone : ""), text: value }));
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
    const QUIET_CHECK = ["done", "skipped", "none"];
    const checkRows = [];
    for (const [kind, label] of KINDS) {
      const st = shot[kind].status;
      if (!QUIET_CHECK.includes(st.state) || st.note) {
        checkRows.push(checkRow(label, st.label, TONE[st.state], fmtNote(st.note)));
      }
    }
    const up = upscaleOf(shot);
    if (up && up.status) {
      const upVal = `${up.scale ? "x" + up.scale + " " : ""}${up.status.label}`.trim();
      checkRows.push(checkRow("视频超分", upVal, TONE[up.status.state], fmtNote(up.status.note)));
    }
    const overrides = [
      shot.video.duration !== null && shot.video.duration !== undefined && `${shot.video.duration}s`,
      shot.video.resolution !== null && shot.video.resolution !== undefined && `分辨率 ${shot.video.resolution}`,
    ].filter(Boolean);
    if (overrides.length) checkRows.push(checkRow("单镜视频参数", overrides.join(" · "), "", ""));
    for (const row of [
      measure("音高", shot.audio.f0, "Hz", limits.f0_max),
      measure("声道差", shot.audio.channel_gap, "dB", limits.channel_gap_max),
      measure("底噪", shot.audio.noise_floor, "dB", limits.noise_floor_max),
    ]) if (row) checkRows.push(row);
    if (webs.length) {
      checkRows.push(checkRow("网页副本", differs.length ? "不同" : "一致", differs.length ? "warn" : "ok",
        (differs.length ? differs : webs).map(([, short]) => short).join("、")));
    }
    if (!checkRows.length) checkRows.push(checkRow("状态", "各项正常", "ok", ""));

    put(info,
      el("div", { class: "drawer-head" },
        el("span", { class: "cap", text: `镜头 ${shot.key} · 提示词与检查` }),
        el("button", { class: "close", "aria-label": "收起抽屉", text: "×", onclick: () => setDrawer(false) })),
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
        el("div", { class: "checks" }, checkRows)),
      el("div", { class: "info-foot", text: "只读：选图用 wrally keyframe pick，改提示词后 wrally push" }));
  }

  function focusShot(key) {
    const shot = byKey.get(key);
    if (!shot) return;
    if (key !== current.key) {
      current.cand = null;
      current.videoSource = "upscaled";
    }
    current.key = key;
    try { history.replaceState(null, "", "#" + key); } catch (e) { /* 受限环境忽略 */ }
    for (const b of rail.querySelectorAll(".shot")) b.setAttribute("aria-current", String(b.dataset.shot === key));
    revealIn(rail, rail.querySelector('[aria-current="true"]'));
    if (current.media === "video" && !hasPlayableVideo(shot)) current.media = "keyframe";
    drawDetail(shot);
    drawInfo(shot);
    // 相邻两镜只预载封面与尾帧，避免把全部候选图一次性拉爆网络与内存
    const list = shown(), at = list.indexOf(shot);
    for (const near of [list[at - 1], list[at + 1]]) {
      if (!near) continue;
      for (const src of [cover(near), near.keyframe.last_src]) {
        if (src) new Image().src = src;
      }
    }
  }
  function nothing() {
    current.key = null;
    put(detail, el("div", { class: "frame-empty" }, el("span", { text: "这一集还没有镜头" })));
    put(info);
  }
