  // ---- 头部 -------------------------------------------------------------------
  document.title = [data.name.toUpperCase(), data.title].filter(Boolean).join(" · ");
  const counts = Object.fromEntries(KINDS.map(([kind]) => {
    const wanted = live.filter(s => stateOf(s, kind) !== "skipped");
    return [kind, [wanted.filter(s => stateOf(s, kind) === "done").length, wanted.length]];
  }));
  const themeButton = el("button", { class: "theme-btn", "aria-label": "切换浅色 / 深色主题", onclick: () => {
    const cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    const next = cur === "dark" ? "light" : "dark";
    applyTheme(next);
    saveTheme(next);
    syncThemeButton();
  } });
  function syncThemeButton() {
    const isDark = currentTheme === "dark";
    themeButton.title = isDark ? "切换到浅色主题" : "切换到深色主题";
    put(themeButton, icon(isDark ? "sun" : "moon"), el("span", { text: isDark ? "浅色" : "深色" }));
  }
  syncThemeButton();
  const filmButton = data.film && el("button", { class: "film-btn", title: "播放整集成片", onclick: () => openFilm() },
    el("span", { class: "ico" }, icon("play")), el("span", { text: "播放成片" }), el("span", { class: "dur" }));
  if (filmButton) {
    const probe = el("video", { src: data.film, preload: "metadata", muted: true });
    probe.addEventListener("loadedmetadata", () => { filmButton.querySelector(".dur").textContent = fmt(probe.duration); });
  }
  document.getElementById("top").append(
    el("div", { class: "head-text" },
      el("div", { class: "eyebrow cap" },
        data.index && el("a", { class: "back", href: data.index, text: "‹ 全部剧集" }),
        data.index ? data.episodes.map(name => el("a", { href: `../${encodeURIComponent(name)}/page.html`, text: name,
          "aria-current": name === data.name ? "page" : null })) : el("span", { class: "name", text: data.name })),
      el("h1", { text: data.title || data.name })),
    el("div", { class: "head-right" },
      el("p", { class: "meta", text: [`${live.length} 镜`, ...KINDS.map(([k, l]) => `${l} ${counts[k].join("/")}`)].join(" · ") }),
      themeButton,
      filmButton));

  // ---- 概览条：整体进度、当前阶段、要处理的镜头、生成方式、各阶段 -----------------
  (() => {
    let done = 0, total = 0;
    for (const [kind] of KINDS) { done += counts[kind][0]; total += counts[kind][1]; }
    const webDiffCount = live.filter(webDiffers).length;
    const flagged = live.filter(needsWork).length;
    if (webDiffCount > 0) {
      done += live.length - webDiffCount;
      total += live.length;
    }
    let pct = total ? Math.round(done / total * 100) : 0;
    if ((done < total || flagged > 0) && pct >= 100) pct = 99;
    const kfWanted = live.filter(s => stateOf(s, "keyframe") !== "skipped");
    const need = (s, kind, p) => stateOf(s, kind) === "skipped" || Boolean(s.prompts[p]);
    const stages = [
      ["提示词", live.filter(s => need(s, "keyframe", "image") && need(s, "video", "anim")).length, live.length],
      ["关键帧", kfWanted.filter(s => s.keyframe.candidates.length || s.keyframe.src || s.keyframe.pick).length, kfWanted.length],
      ["选图", kfWanted.filter(s => ["done", "missing"].includes(stateOf(s, "keyframe"))).length, kfWanted.length],
      ["视频", ...counts.video], ["旁白", ...counts.audio], ["成片", data.film ? 1 : 0, 1],
    ];
    if (webDiffCount > 0) stages.push(["网页同步", live.length - webDiffCount, live.length]);
    const now = stages.find(([, a, b]) => a < b);
    const stageText = now ? `${now[0]} ${now[1]}/${now[2]}` : flagged ? `待处理 ${flagged} 镜` : "全部完成";
    const pairs = live.filter(isPair).length;
    document.getElementById("overview").append(
      el("div", { class: "ov-main" }, el("span", { class: "pct" }, String(pct), el("i", { text: "%" })), el("span", { class: "cap", text: "整体进度" })),
      el("div", { class: "track", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 },
        el("i", { style: `width:${pct}%` })),
      el("div", { class: "ov-stats" },
        el("div", { class: "stat" }, el("span", { class: "cap", text: "当前阶段" }), el("b", { class: !now && flagged ? "warn" : null, text: stageText })),
        el("div", { class: "stat" }, el("span", { class: "cap", text: "需要处理" }),
          el("b", { class: flagged || data.note ? "warn" : null, text: data.note ? "公共文本读取失败" : flagged ? `${flagged} 镜` : "没有" })),
        el("div", { class: "stat" }, el("span", { class: "cap", text: "生成方式" }), el("b", { text: `首尾帧 ${pairs} · 单图 ${live.length - pairs}` }))),
      el("div", { class: "chapters" }, stages.map(([name, a, b]) =>
        el("span", { class: a >= b ? "done" : now && now[0] === name ? "now" : null, title: `${name} ${a}/${b}`, text: name }))));
  })();

  // ---- 筛选和镜头列表 ---------------------------------------------------------
  const FILTERS = [["all", "全部", () => true], ["open", "未完成", isOpen], ["warn", "需处理", needsWork],
    ["running", "生成中", hasState("running")], ["failed", "失败", hasState("failed")]];
  const current = { filter: "all", key: null, media: "keyframe", videoSource: "upscaled" };
  const shown = () => data.shots.filter((FILTERS.find(([f]) => f === current.filter) || FILTERS[0])[2]);
  const byKey = new Map(data.shots.map(s => [s.key, s]));
  const filters = document.getElementById("filters");
  filters.append(...segment(FILTERS.map(([value, label, test]) => [value, label, el("i", { text: String(data.shots.filter(test).length) })]),
    current.filter, value => applyFilter(value)));

  const rail = document.getElementById("rail");
  function railItem(shot) {
    const kf = shot.keyframe;
    const thumb = cover(shot) || (kf.candidates.find(c => c.src) || {}).src;
    const [label, tone] = badge(shot);
    const hasVideo = Boolean(shot.video.src || (upscaleOf(shot) && upscaleOf(shot).src));
    const pipTitle = KINDS.map(([kind, klabel]) => `${klabel}:${shot[kind].status.label}`).join(" · ");
    const pips = el("span", { class: "pips", title: pipTitle, "aria-label": pipTitle },
      KINDS.map(([kind]) => el("span", { class: "pip " + (shot.cut ? "idle" : TONE[stateOf(shot, kind)]) })));
    const statusCls = "shot-status" + (tone === "bad" ? " bad" : tone === "warn" ? " warn" : tone === "run" ? " run" : "");
    return el("button", { class: "shot" + (shot.cut ? " cut" : ""), "data-shot": shot.key, "data-open": isOpen(shot) ? "1" : "0",
      "aria-current": String(shot.key === current.key), title: `${shot.key} · ${label}（${pipTitle}）`, onclick: () => focusShot(shot.key) },
      el("span", { class: "thumb" }, thumb && el("img", { src: thumb, alt: "", loading: "lazy" }),
        hasVideo && el("span", { class: "has-video" })),
      el("span", { class: "shot-cap" },
        el("span", { class: "shot-line" }, el("span", { class: "shot-key", text: shot.key }),
          el("span", { class: "shot-name", text: shot.narration || "（没有台词）" })),
        el("span", { class: "shot-meta" }, pips,
          el("span", { class: "mode" + (isPair(shot) ? " pair" : ""), text: isPair(shot) ? "首尾帧" : "单图" }),
          el("span", { class: statusCls, text: label }))));
  }
  function drawRail() {
    const list = shown();
    put(rail, ...(list.length ? list.map(railItem) : [el("div", { class: "rail-empty", text: "这一筛选下没有镜头" })]));
    revealIn(rail, rail.querySelector('[aria-current="true"]'));
  }
  function applyFilter(which) {
    current.filter = which;
    pressSeg(filters, which);
    const list = shown();
    drawRail();
    // 当前这镜被筛掉了：换到后面第一个还在的，后面没有就取最后一个
    if (list.length && !list.some(s => s.key === current.key)) {
      const at = data.shots.findIndex(s => s.key === current.key);
      focusShot((list.find(s => data.shots.indexOf(s) > at) || list[list.length - 1]).key);
    }
  }
