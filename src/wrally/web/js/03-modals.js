  // ---- 浮层 ---------------------------------------------------------------------
  const host = document.getElementById("modal");
  let modal = null;  // 打开着的浮层：{ step, kind }
  function openModal(node, extra) {
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

  // 候选大图：左右切换，底下给出选图的命令（页面不改清单）
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
    let at = Math.max(0, Math.min(items.length - 1, start));
    const eyebrow = el("div", { class: "cap" }), title = el("h2", { class: "modal-title" });
    const img = el("img", { alt: "", onload: orient });
    const prev = el("button", { class: "nav-btn prev", "aria-label": "上一张", text: "‹", onclick: () => go(at - 1) });
    const next = el("button", { class: "nav-btn next", "aria-label": "下一张", text: "›", onclick: () => go(at + 1) });
    const meta = el("div", { class: "lb-meta" });
    const thumbs = items.map((c, i) => el("button", { class: "cand" + (c.picked ? " picked" : c.last ? " last" : c.suggested ? " suggested" : ""),
      title: candName(c), onclick: () => go(i) }, el("img", { src: c.src, alt: "" }), el("span", { class: "a", text: c.label || "选用" })));
    const strip = el("div", { class: "lb-thumbs" }, thumbs);
    const foot = el("div", { class: "modal-foot" });
    function go(i) {
      if (i < 0 || i >= items.length) return;
      at = i;
      const c = items[at], kf = shot.keyframe;
      eyebrow.textContent = `镜头 ${shot.key} · ${modeName(shot)} · ${at + 1} / ${items.length}`;
      title.textContent = candName(c);
      img.src = c.src;
      prev.disabled = at === 0;
      next.disabled = at === items.length - 1;
      put(meta, ...candTags(c), c.suggested && kf.suggest_note && el("span", { class: "note", text: kf.suggest_note }));
      thumbs.forEach((t, j) => t.classList.toggle("on", j === at));
      revealIn(strip, thumbs[at]);
      put(foot,
        c.label && !c.picked ? command(`wrally keyframe pick ${shot.key} ${c.label}`, "复制") : el("span", { text: c.picked ? "这一张是选用图" : "" }),
        c.label && !c.picked && !c.last ? command(`wrally keyframe pick ${shot.key} ${c.label} --last`, "复制") : null,
        el("span", { class: "grow" }), el("span", { text: "页面不改清单，选图用命令" }));
      // 当前候选记下来，刷新后接着看这张
      current.cand = c.label;
    }
    openModal(el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      el("div", { class: "modal-head" }, el("div", { style: "min-width:0" }, eyebrow, title),
        el("button", { class: "close", "aria-label": "关闭", text: "×", onclick: closeModal })),
      el("div", { class: "stage" }, img, prev, next), meta, strip, foot), { step: d => go(at + d) });
    go(at);
  }
  function openFilm() {
    openModal(el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      head(`${data.name} · 成片`, data.title || data.name),
      el("div", { class: "stage" }, el("video", { src: data.film, controls: true, autoplay: true, playsinline: true, preload: "metadata", onloadedmetadata: orient })),
      el("div", { class: "modal-foot" }, el("span", { text: "改过素材后要重新合成、再跑一次 wrally page，这里才会更新" }))));
  }
