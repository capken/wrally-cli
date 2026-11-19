  const VERSION = "__WRALLY_VERSION__";
  const data = JSON.parse(document.getElementById("data").textContent);
  const KINDS = [["keyframe", "关键帧"], ["video", "视频"], ["audio", "旁白"]];
  const PROMPTS = [["image", "图片", "图片提示词"], ["anim", "动画", "动画提示词"], ["audio", "旁白", "旁白提示词"]];
  const SETTLED = ["done", "skipped"];
  const BAD = ["failed", "stale", "missing"];
  // 状态 → 颜色：ok 绿、warn 橙、bad 红、run 蓝、idle 灰
  const TONE = { done: "ok", skipped: "ok", pending: "warn", stale: "warn", missing: "warn", failed: "bad", running: "run", none: "idle", cut: "idle" };

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
  const PATHS = { play: "M4.2 2.1 13.6 8l-9.4 5.9z", pause: "M3.4 2.4h3.2v11.2H3.4zM9.4 2.4h3.2v11.2H9.4z" };
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
  const webDiffers = shot => Object.values(shot.prompts).some(p => p && p.web === "differs");
  const isOpen = shot => !shot.cut
    && (webDiffers(shot) || KINDS.some(([kind]) => !SETTLED.includes(stateOf(shot, kind))));
  const needsWork = shot => !shot.cut && (webDiffers(shot) || KINDS.some(([kind]) => BAD.includes(stateOf(shot, kind))));
  const hasState = state => shot => !shot.cut && KINDS.some(([kind]) => stateOf(shot, kind) === state);
  const isPair = shot => Boolean(shot.keyframe.last);
  function worst(shot) {
    if (shot.cut) return "cut";
    const states = KINDS.map(([kind]) => stateOf(shot, kind));
    for (const s of ["failed", "stale", "missing", "running", "pending", "none"]) if (states.includes(s)) return s;
    return webDiffers(shot) ? "stale" : "done";
  }
  function badge(shot) {
    const w = worst(shot);
    if (w === "cut") return ["不进成片", "idle"];
    if (w === "done") return ["完成", "ok"];
    if (w === "stale" && KINDS.every(([kind]) => SETTLED.includes(stateOf(shot, kind)))) return ["网页副本不同", "warn"];
    const [kind, label] = KINDS.find(([k]) => stateOf(shot, k) === w);
    return [`${label}${shot[kind].status.label}`, TONE[w]];
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

  function copyButton(text, label) {
    const button = el("button", { class: "copy", text: label, onclick: e => {
      e.stopPropagation();
      navigator.clipboard && navigator.clipboard.writeText(text).then(() => {
        button.textContent = "已复制";
        setTimeout(() => { button.textContent = label; }, 1500);
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
