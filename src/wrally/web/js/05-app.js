  // 一次只响一个：开始播放时停掉其他的
  document.addEventListener("play", e => {
    for (const m of document.querySelectorAll("video, audio")) if (m !== e.target) m.pause();
  }, true);

  // 键盘：J / K 上下一镜，V 换关键帧 / 视频；大图里左右换候选；Esc 关浮层
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (modal) {
      if (e.key === "Escape") closeModal();
      else if (modal.step && (e.key === "ArrowRight" || e.key === "ArrowLeft")) { e.preventDefault(); modal.step(e.key === "ArrowRight" ? 1 : -1); }
      return;
    }
    // 焦点在播放器上时，按键留给它
    if (e.target instanceof Element && e.target.matches("video, audio")) return;
    const step = { j: 1, k: -1 }[e.key.toLowerCase()];
    if (step) {
      const list = shown(), at = list.findIndex(s => s.key === current.key);
      const next = list[Math.min(list.length - 1, Math.max(0, at + step))];
      if (next && next.key !== current.key) focusShot(next.key);
    } else if (e.key.toLowerCase() === "v") {
      const shot = byKey.get(current.key);
      if (shot && shot.video.src) setMedia(current.media === "video" ? "keyframe" : "video");
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const shot = byKey.get(current.key);
      if (!shot) return;
      e.preventDefault();
      const items = candidates(shot), src = cover(shot);
      openCand(shot, Math.max(0, items.findIndex(c => c.src === src)));
    }
  });

  // ---- 底栏 ---------------------------------------------------------------------
  const fresh = el("span", { class: "fresh" });
  const running = live.filter(hasState("running")).length;
  put(document.getElementById("foot"), el("span", { class: "ro", text: "只读视图" }), el("span", { text: "·" }), fresh,
    data.note && el("span", { class: "note", text: "· 公共文本读取失败：" + data.note }),
    el("span", { class: "grow" }), el("span", { text: running ? `${running} 镜生成中` : "无进行中任务" }),
    el("span", { class: "keys" }, " · ", el("kbd", { text: "J" }), " ", el("kbd", { text: "K" }), " 上下一镜 ",
      el("kbd", { text: "V" }), " 视频 ", el("kbd", { text: "←" }), " ", el("kbd", { text: "→" }), " 候选大图"));

  // 刷新前后保持筛选、当前镜头、关键帧 / 视频和正在看的那张候选
  const STORE = "wrally-page:" + location.pathname;
  window.addEventListener("pagehide", () => {
    try {
      sessionStorage.setItem(STORE, JSON.stringify({
        filter: current.filter, key: current.key, media: current.media, shape,
        cand: modal && modal.step ? current.cand : null,
      }));
    } catch (e) { /* 浏览器不给用就算了 */ }
  });
  (() => {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(STORE) || "null"); } catch (e) { /* 同上 */ }
    if (saved) {
      current.media = saved.media === "video" ? "video" : "keyframe";
      setShape(saved.shape);
    }
    const hash = location.hash.replace(/^#s?/, "");
    current.key = saved && byKey.has(saved.key) ? saved.key : byKey.has(hash) ? hash : null;
    current.filter = saved && FILTERS.some(([f]) => f === saved.filter) ? saved.filter : "all";
    pressSeg(filters, current.filter);
    drawRail();
    const list = shown();
    const start = byKey.get(current.key) || list[0] || data.shots[0];
    if (!start) { nothing(); return; }
    focusShot(start.key);
    if (saved && saved.cand !== null && saved.cand !== undefined) {
      const items = candidates(start), at = items.findIndex(c => c.label === saved.cand);
      if (at >= 0) openCand(start, at);
    }
  })();
  requestAnimationFrame(syncSegs);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncSegs);
  window.addEventListener("resize", syncSegs);

  // 发现更新：file:// 下不能 fetch，所以定时用 script 标签加载版本文件，它会调用 __wrallyPage
  const stamp = new Date(document.lastModified);
  if (!isNaN(stamp)) fresh.textContent = "更新于 " + stamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  let offered = false;
  const busy = () => (modal && !modal.step) || [...document.querySelectorAll("video, audio")].some(m => !m.paused && !m.ended);
  window.__wrallyPage = version => {
    if (version === VERSION) return;
    if (!busy()) { location.reload(); return; }
    if (offered) return;
    offered = true;  // 正在播放或看提示词全文时不打断，等人来点
    put(fresh, el("button", { text: "有更新 · 点击刷新", onclick: () => location.reload() }));
  };
  setInterval(() => {
    const probe = el("script", { src: ".wrally/page.version.js?t=" + Date.now() });
    probe.onload = probe.onerror = () => probe.remove();
    document.head.append(probe);
  }, 3000);
