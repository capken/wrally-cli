  const VERSION = "__WRALLY_VERSION__";
  const data = JSON.parse(document.getElementById("data").textContent);
  const KINDS = [["keyframe", "关键帧"], ["video", "视频"], ["audio", "旁白"]];
  const PROMPTS = [["image", "图片", "图片提示词"], ["anim", "动画", "动画提示词"], ["audio", "旁白", "旁白提示词"]];
  const SETTLED = ["done", "skipped"];
  const BAD = ["failed", "stale", "missing"];
  // 状态 → 颜色：ok 绿、warn 橙、bad 红、run 蓝、idle 灰
  const TONE = { done: "ok", skipped: "ok", pending: "warn", stale: "warn", missing: "warn", failed: "bad", running: "run", none: "idle", cut: "idle" };
  const STALE_ZH = {
    "video was made from a different keyframe or prompt": "关键帧或动画提示词已改动",
    "audio was made from different narration": "台词、模型或音色已改动",
    "upscale was made from an older video": "原视频已更新，超分版已过期",
  };
  const fmtNote = note => (note && STALE_ZH[note]) || note || "";

  // 主题：默认深色审片室，可手动切换并尽量记住（file:// 下容错）
  const THEME_KEY = "wrally-theme";
  function readTheme() {
    try {
      const v = localStorage.getItem(THEME_KEY);
      if (v === "light" || v === "dark") return v;
    } catch (e) { /* 浏览器或 file:// 禁用时回退 */ }
    try {
      const v = sessionStorage.getItem(THEME_KEY);
      if (v === "light" || v === "dark") return v;
    } catch (e) { /* 同上 */ }
    return "dark";
  }
  function saveTheme(theme) {
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* 忽略 */ }
    try { sessionStorage.setItem(THEME_KEY, theme); } catch (e) { /* 忽略 */ }
  }
  let currentTheme = readTheme();
  function applyTheme(theme) {
    currentTheme = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", currentTheme);
  }
  applyTheme(currentTheme);

  // 所有文字都经 textContent 写入，提示词里的任何字符都不会被当成标签
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children.flat()) if (child) node.append(child);
    return node;
  }
  // 换掉 node 的全部子节点；null、false 这些空位跳过（replaceChildren 会把它们写成文字）
  const put = (node, ...children) => node.replaceChildren(...children.flat().filter(Boolean));
  const PATHS = {
    play: "M4.2 2.1 13.6 8l-9.4 5.9z",
    pause: "M3.4 2.4h3.2v11.2H3.4zM9.4 2.4h3.2v11.2H9.4z",
    sun: "M8 4.8a3.2 3.2 0 1 0 0 6.4 3.2 3.2 0 0 0 0-6.4zM8 1.2a.7.7 0 0 1 .7.7V3a.7.7 0 0 1-1.4 0V1.9a.7.7 0 0 1 .7-.7zm0 11.1a.7.7 0 0 1 .7.7v1.1a.7.7 0 0 1-1.4 0V13a.7.7 0 0 1 .7-.7zM1.2 8a.7.7 0 0 1 .7-.7H3a.7.7 0 0 1 0 1.4H1.9a.7.7 0 0 1-.7-.7zm11.1 0a.7.7 0 0 1 .7-.7h1.1a.7.7 0 0 1 0 1.4H13a.7.7 0 0 1-.7-.7zM3.2 3.2a.7.7 0 0 1 1 0l.8.8a.7.7 0 1 1-1 1l-.8-.8a.7.7 0 0 1 0-1zm7.8 7.8a.7.7 0 0 1 1 0l.8.8a.7.7 0 1 1-1 1l-.8-.8a.7.7 0 0 1 0-1zm1.8-7.8a.7.7 0 0 1 0 1l-.8.8a.7.7 0 1 1-1-1l.8-.8a.7.7 0 0 1 1 0zM5 11a.7.7 0 0 1 0 1l-.8.8a.7.7 0 1 1-1-1l.8-.8a.7.7 0 0 1 1 0z",
    moon: "M11.8 10.6A5.4 5.4 0 0 1 5.4 4.2a5.4 5.4 0 0 1 .4-2.1 6 6 0 1 0 8.1 8.1 5.4 5.4 0 0 1-2.1.4z",
  };
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", PATHS[name]);
    svg.append(path);
    return svg;
  }
  const fmt = t => { const v = Math.max(0, Math.floor(t || 0)); return Math.floor(v / 60) + ":" + String(v % 60).padStart(2, "0"); };

  const stateOf = (shot, kind) => shot[kind].status.state;
  const upscaleOf = shot => (shot.video && shot.video.upscaled) || null;
  const upscaleBad = shot => Boolean(upscaleOf(shot) && upscaleOf(shot).status && BAD.includes(upscaleOf(shot).status.state));
  const webDiffers = shot => Object.values(shot.prompts).some(p => p && p.web === "differs");
  const isOpen = shot => !shot.cut
    && (webDiffers(shot) || upscaleBad(shot) || KINDS.some(([kind]) => !SETTLED.includes(stateOf(shot, kind))));
  const needsWork = shot => !shot.cut
    && (webDiffers(shot) || upscaleBad(shot) || KINDS.some(([kind]) => BAD.includes(stateOf(shot, kind))));
  const hasState = state => shot => !shot.cut
    && (KINDS.some(([kind]) => stateOf(shot, kind) === state)
      || Boolean(upscaleOf(shot) && upscaleOf(shot).status && upscaleOf(shot).status.state === state));
  const isPair = shot => Boolean(shot.keyframe.last);
  function worst(shot) {
    if (shot.cut) return "cut";
    const states = KINDS.map(([kind]) => stateOf(shot, kind));
    for (const s of ["failed", "stale", "missing", "running", "pending", "none"]) if (states.includes(s)) return s;
    if (upscaleBad(shot)) return upscaleOf(shot).status.state;
    return webDiffers(shot) ? "stale" : "done";
  }
  function badge(shot) {
    const w = worst(shot);
    if (w === "cut") return ["不进成片", "idle"];
    if (w === "done") return ["完成", "ok"];
    const match = KINDS.find(([k]) => stateOf(shot, k) === w);
    if (match) return [`${match[1]}${shot[match[0]].status.label}`, TONE[w]];
    if (upscaleBad(shot)) return [`超分${upscaleOf(shot).status.label}`, TONE[w]];
    return ["网页副本不同", "warn"];
  }
  // 画面位置放哪张：选用图；没选时放推荐的那张
  function cover(shot) {
    const kf = shot.keyframe;
    if (kf.src) return kf.src;
    const hinted = kf.candidates.find(c => c.suggested && c.src);
    return !kf.pick && hinted ? hinted.src : null;
  }
  const live = data.shots.filter(s => !s.cut);  // 不进成片的镜头不算进度

  // 画面比例：取载入的图或视频的实际宽高
  let shape = null;
  function setShape(value) {
    if (!value || value === shape) return;
    shape = value;
    document.documentElement.style.setProperty("--ar", value);
    document.documentElement.classList.toggle("portrait", Number(value) < 1);
  }
  function orient(e) {
    const m = e.target;
    const w = m.naturalWidth || m.videoWidth, h = m.naturalHeight || m.videoHeight;
    if (w && h) setShape(String(Math.round(w / h * 1000) / 1000));
  }

  // 只滚动 box 自己，把 item 露出来。scrollIntoView 会连带整页一起滚
  function revealIn(box, item) {
    if (!item) return;
    const b = box.getBoundingClientRect(), r = item.getBoundingClientRect();
    box.scrollLeft += r.left < b.left ? r.left - b.left : r.right > b.right ? r.right - b.right : 0;
    box.scrollTop += r.top < b.top ? r.top - b.top : r.bottom > b.bottom ? r.bottom - b.bottom : 0;
  }

  // 三级剪贴板回退：Clipboard API -> 隐藏 textarea + execCommand -> 手动选中提示
  function copyExecCommand(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0;";
    document.body.append(ta);
    let ok = false;
    try {
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = Boolean(document.execCommand && document.execCommand("copy"));
    } catch (e) {
      ok = false;
    } finally {
      ta.remove();
    }
    return ok;
  }
  function copyTextSafe(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text).then(() => true, () => copyExecCommand(text));
    }
    return Promise.resolve(copyExecCommand(text));
  }
  function selectCodeNear(button) {
    const wrap = button.closest(".cmd");
    const code = wrap && wrap.querySelector("code");
    if (!code || !window.getSelection) return;
    try {
      const range = document.createRange();
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) { /* 忽略 */ }
  }
  function copyButton(text, label) {
    let timer = null;
    const button = el("button", { class: "copy", text: label, onclick: e => {
      e.stopPropagation();
      copyTextSafe(text).then(ok => {
        if (timer) clearTimeout(timer);
        if (ok) {
          button.textContent = "已复制";
          timer = setTimeout(() => { button.textContent = label; }, 1500);
        } else {
          button.textContent = "请手动复制";
          selectCodeNear(button);
          timer = setTimeout(() => { button.textContent = label; }, 2400);
        }
      });
    } });
    return button;
  }
  const command = (text, label) => el("span", { class: "cmd" }, el("code", { title: "在单集目录下运行", text }), copyButton(text, label));

  // 分段控件：滑块挪到选中的那一项下面
  function syncSegs() {
    for (const seg of document.querySelectorAll(".seg")) {
      const thumb = seg.querySelector(".seg-thumb"), item = seg.querySelector('.seg-item[aria-pressed="true"]');
      if (!thumb) continue;
      thumb.style.opacity = item ? "1" : "0";
      if (item) { thumb.style.left = item.offsetLeft + "px"; thumb.style.width = item.offsetWidth + "px"; }
    }
  }
  function segment(items, chosen, onpick) {
    return [el("span", { class: "seg-thumb", "aria-hidden": "true" }), ...items.map(([value, ...label]) =>
      el("button", { class: "seg-item", "data-value": value, "aria-pressed": String(value === chosen),
        onclick: () => onpick(value) }, ...label))];
  }
  function pressSeg(seg, value) {
    for (const b of seg.querySelectorAll(".seg-item")) b.setAttribute("aria-pressed", String(b.dataset.value === value));
    syncSegs();
  }
