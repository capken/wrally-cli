  // ---- 浮层 ---------------------------------------------------------------------
  const host = document.getElementById("modal");
  const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  let modal = null;  // 打开着的浮层：{ kind, step, toggleCompare, toggleGrid }
  let modalTrigger = null;

  function trapModalFocus(e) {
    if (!modal || e.key !== "Tab") return;
    const box = host.querySelector(".modal");
    if (!box) return;
    const nodes = [...box.querySelectorAll(FOCUSABLE)].filter(n => n.offsetParent !== null);
    if (!nodes.length) { e.preventDefault(); return; }
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first || !box.contains(document.activeElement)) {
        e.preventDefault();
        last.focus();
      }
    } else if (document.activeElement === last || !box.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    }
  }
  function openModal(node, extra) {
    if (!modal) modalTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    for (const m of document.querySelectorAll("video, audio")) m.pause();
    put(host, node);
    host.classList.add("open");
    modal = extra || {};
    syncSegs();
    const button = host.querySelector(".close");
    if (button) button.focus({ preventScroll: true });
  }
  function closeModal() {
    if (!modal) return;
    host.classList.remove("open");
    put(host);
    modal = null;
    const prev = modalTrigger;
    modalTrigger = null;
    if (prev && prev.isConnected) prev.focus({ preventScroll: true });
  }
  host.addEventListener("click", e => { if (e.target === host) closeModal(); });
  const head = (eyebrow, title) => el("div", { class: "modal-head" },
    el("div", { style: "min-width:0" }, el("div", { class: "cap", text: eyebrow }), el("h2", { class: "modal-title", text: title })),
    el("button", { class: "close", "aria-label": "关闭", text: "×", onclick: closeModal }));
  const modeName = shot => isPair(shot) ? "首尾帧" : "单图";

  // 提示词全文：灰色是公共文本，网页上编辑过的另有提醒
  function promptBody(shot, kind, label) {
    const p = shot.prompts[kind];
    if (!p) return [el("p", { class: "muted", text: "这一镜没有" + label })];
    const what = kind === "image" ? "生图" : "生视频";
    return [
      p.web && el("p", { class: "web-note " + p.web, text: p.web === "differs"
        ? `这一镜的${label}在 WorkRally 网页上编辑过，网页另存的那份和这里不一样。${what}用的是网页那份，push 改不到它：` +
          "请先刷新网页，再把这里的全文粘贴进去保存。改好之前，对应的生成命令会报 stale_web_prompt 中止。"
        : `这一镜的${label}在 WorkRally 网页上编辑过，网页另存着一份副本（目前和这里一致）。${what}用的是那份，push 改不到它：` +
          "以后改这段提示词，除了 push，还要到网页上粘贴成同样的文字。" }),
      el("p", { class: "prompt" },
        p.before && el("span", { class: "common", text: p.before }),
        el("span", { text: p.own }),
        p.after && el("span", { class: "common", text: p.after })),
      p.note && el("p", { class: "prompt-note", text: "拼接失败：" + p.note })];
  }
  function openPrompt(shot, kind) {
    const title = el("h2", { class: "modal-title" });
    const body = el("div", { class: "modal-body" });
    const foot = el("div", { class: "modal-foot" });
    const seg = el("nav", { class: "seg" });
    function show(which) {
      const [, , label] = PROMPTS.find(([k]) => k === which);
      const p = shot.prompts[which];
      title.textContent = label;
      put(body, ...promptBody(shot, which, label).filter(Boolean));
      body.scrollTop = 0;
      put(foot, el("span", { text: !p ? "" : p.raw ? "整段自己写" : p.before || p.after ? "灰色是公共文本" : "" }),
        el("span", { class: "grow" }), p && copyButton(p.before + p.own + p.after, "复制全文"));
      pressSeg(seg, which);
    }
    seg.append(...segment(PROMPTS.map(([k, short]) => [k, short,
      shot.prompts[k] && shot.prompts[k].web && el("span", { class: "web-dot " + shot.prompts[k].web })]), kind, show));
    openModal(el("div", { class: "modal", role: "dialog", "aria-modal": "true" },
      el("div", { class: "modal-head" },
        el("div", { style: "min-width:0" }, el("div", { class: "cap", text: `镜头 ${shot.key} · ${modeName(shot)}` }), title),
        el("button", { class: "close", "aria-label": "关闭", text: "×", onclick: closeModal })),
      seg, body, foot));
    show(kind);
  }

  // 参考图：清单里 keyframe.refs 的镜号和 ref_assets 名字
  const REF_KIND = { shot: "镜头", file: "本地文件", asset: "媒资", unknown: "未登记" };
  function openRefs(shot) {
    const refs = shot.keyframe.refs;
    openModal(el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      head(`镜头 ${shot.key} · ${refs.length} 张参考图`, "参考图"),
      el("div", { class: "modal-body" }, el("div", { class: "ref-grid" }, refs.map(r => el("div", { class: "ref-card" },
        el("div", { class: "pic" }, r.src ? el("img", { src: r.src, alt: "" })
          : r.kind === "shot" ? "那一镜还没有选用图" : r.kind === "asset" ? "媒资，页面上没有图（可在 page.previews 登记预览图）" : "不在 ref_assets 里"),
        el("div", { class: "ref-name", text: r.kind === "shot" ? "镜头 " + r.name : r.name }),
        el("div", { class: "ref-meta" }, el("span", { class: "tag", text: REF_KIND[r.kind] }),
          el("span", { text: r.kind === "shot" ? "那一镜的选用图" : "ref_assets" })))))),
      el("div", { class: "modal-foot" }, el("span", { text: "keyframe gen 用它们当参考图" }))));
  }

  // 候选大图 / 并排对比 / 全览网格：左右切换，底下给出选图命令（页面不改清单）
  function candidates(shot) {
    const kf = shot.keyframe;
    const items = kf.candidates.filter(c => c.src);
    // 选用的图不在候选里（比如直接给的 asset_id）：放在最前面
    if (kf.src && !items.some(c => c.picked)) items.unshift({ src: kf.src, label: "", picked: true, last: false, suggested: false });
    return items;
  }
  const candName = c => c.label ? "候选 " + c.label : "选用关键帧";
  function candTags(c) {
    return [c.picked && el("span", { class: "tag start", text: "选用" }), c.last && el("span", { class: "tag end", text: "尾帧" }),
      c.suggested && el("span", { class: "tag suggested", text: "推荐" })].filter(Boolean);
  }
  function openCand(shot, start) {
    const items = candidates(shot);
    if (!items.length) return;
    const pickedIdx = items.findIndex(c => c.picked);
    const suggestedIdx = items.findIndex(c => c.suggested);
    let anchor = pickedIdx >= 0 ? pickedIdx : suggestedIdx >= 0 ? suggestedIdx : 0;
    let at = Math.max(0, Math.min(items.length - 1, start));
    let view = items.length >= 2 && at !== anchor ? "compare" : "single";

    const eyebrow = el("div", { class: "cap" }), title = el("h2", { class: "modal-title" });
    const modeButtons = items.length >= 2 ? [
      ["single", "单图"],
      ["compare", "并排对比"],
      ["grid", `全览网格 (${items.length})`],
    ].map(([mode, text]) => el("button", { "data-mode": mode, "aria-pressed": String(view === mode), text,
      onclick: () => setView(mode) })) : [];
    const modeBar = modeButtons.length ? el("div", { class: "lb-mode", role: "group", "aria-label": "候选查看模式" }, modeButtons) : null;

    const stageHost = el("div", { style: "flex:1 1 auto;min-height:0;display:flex;flex-direction:column;" });
    const meta = el("div", { class: "lb-meta" });
    const thumbs = items.map((c, i) => el("button", { class: "cand" + (c.picked ? " picked" : c.last ? " last" : c.suggested ? " suggested" : ""),
      title: candName(c), onclick: () => go(i) }, el("img", { src: c.src, alt: "" }), el("span", { class: "a", text: c.label || "选用" })));
    const strip = el("div", { class: "lb-thumbs" }, thumbs);
    const foot = el("div", { class: "modal-foot" });

    function setView(next) {
      if (items.length < 2 && next !== "single") return;
      if (next === "compare" && at === anchor && items.length >= 2) at = (anchor + 1) % items.length;
      view = next;
      render();
    }
    function go(i) {
      if (i < 0 || i >= items.length) return;
      at = i;
      render();
    }
    function renderFoot(c) {
      put(foot,
        c.label && !c.picked ? command(`wrally keyframe pick ${shot.key} ${c.label}`, "复制") : el("span", { text: c.picked ? "这一张是选用图" : "" }),
        c.label && !c.picked && !c.last ? command(`wrally keyframe pick ${shot.key} ${c.label} --last`, "复制") : null,
        el("span", { class: "grow" }), el("span", { text: items.length >= 2 ? "C 对比 · G 网格 · 选图用命令" : "页面不改清单，选图用命令" }));
    }
    function render() {
      const c = items[at], left = items[anchor], kf = shot.keyframe;
      for (const b of modeButtons) b.setAttribute("aria-pressed", String(b.dataset.mode === view));
      eyebrow.textContent = `镜头 ${shot.key} · ${modeName(shot)} · ${at + 1} / ${items.length}`;
      if (view === "single") {
        title.textContent = candName(c);
        const prev = el("button", { class: "nav-btn prev", "aria-label": "上一张", text: "‹", disabled: at === 0, onclick: () => go(at - 1) });
        const next = el("button", { class: "nav-btn next", "aria-label": "下一张", text: "›", disabled: at === items.length - 1, onclick: () => go(at + 1) });
        put(stageHost, el("div", { class: "stage" }, el("img", { src: c.src, alt: "", onload: orient }), prev, next));
        put(meta, ...candTags(c), c.suggested && kf.suggest_note && el("span", { class: "note", text: kf.suggest_note }));
        meta.classList.remove("hidden");
        strip.classList.remove("hidden");
      } else if (view === "compare") {
        title.textContent = `${candName(left)} 对比 ${candName(c)}`;
        const prev = el("button", { class: "nav-btn prev", "aria-label": "上一张候选", text: "‹", disabled: at === 0, onclick: () => go(at - 1) });
        const next = el("button", { class: "nav-btn next", "aria-label": "下一张候选", text: "›", disabled: at === items.length - 1, onclick: () => go(at + 1) });
        const pinBtn = at !== anchor ? el("button", { class: "pin-btn", text: "设为基准", onclick: () => { anchor = at; render(); } }) : null;
        put(stageHost, el("div", { class: "compare-stage" },
          el("div", { class: "compare-pane anchor" },
            el("div", { class: "compare-cap" }, el("span", { class: "compare-label", text: `基准：${candName(left)}` }), ...candTags(left)),
            el("div", { class: "compare-pic" }, el("img", { src: left.src, alt: "", onload: orient }))),
          el("div", { class: "compare-pane active" },
            el("div", { class: "compare-cap" }, el("span", { class: "compare-label", text: `候选：${candName(c)}` }), ...candTags(c), pinBtn),
            el("div", { class: "compare-pic" }, el("img", { src: c.src, alt: "", onload: orient }), prev, next))));
        const note = c.suggested && kf.suggest_note ? kf.suggest_note : left.suggested && kf.suggest_note ? `基准推荐：${kf.suggest_note}` : "";
        put(meta, note && el("span", { class: "note", text: note }));
        meta.classList.toggle("hidden", !note);
        strip.classList.remove("hidden");
      } else {
        title.textContent = `全部候选（${items.length} 张）`;
        put(stageHost, el("div", { class: "compare-grid" }, items.map((item, i) =>
          el("button", { class: "grid-card" + (i === at ? " on" : ""), onclick: () => {
            at = i;
            view = i === anchor ? "single" : "compare";
            render();
          } },
            el("div", { class: "grid-pic" }, el("img", { src: item.src, alt: "", loading: "lazy", onload: orient })),
            el("div", { class: "grid-cap" }, el("span", { class: "grid-name", text: candName(item) }),
              i === anchor && items.length >= 2 && el("span", { class: "tag", text: "基准" }),
              ...candTags(item))))));
        put(meta, el("span", { class: "note", text: "点任意一张进入并排对比，点基准图查看单图大图" }));
        meta.classList.remove("hidden");
        strip.classList.add("hidden");
      }
      thumbs.forEach((t, j) => t.classList.toggle("on", j === at));
      if (view !== "grid") revealIn(strip, thumbs[at]);
      renderFoot(c);
      current.cand = c.label;
    }

    openModal(el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      el("div", { class: "modal-head" },
        el("div", { style: "min-width:0" }, eyebrow, title),
        el("div", { class: "modal-head-actions" }, modeBar,
          el("button", { class: "close", "aria-label": "关闭", text: "×", onclick: closeModal }))),
      stageHost, meta, strip, foot),
      {
        kind: "cand",
        step: d => go(at + d),
        toggleCompare: () => { if (items.length >= 2) setView(view === "compare" ? "single" : "compare"); },
        toggleGrid: () => { if (items.length >= 2) setView(view === "grid" ? "compare" : "grid"); },
      });
    render();
  }
  function openFilm() {
    openModal(el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      head(`${data.name} · 成片`, data.title || data.name),
      el("div", { class: "stage" }, el("video", { src: data.film, controls: true, autoplay: true, playsinline: true, preload: "metadata", onloadedmetadata: orient })),
      el("div", { class: "modal-foot" }, el("span", { text: "改过素材后要重新合成、再跑一次 wrally page，这里才会更新" }))));
  }
