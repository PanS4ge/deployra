/* Deployra — spoiwo UI: toasty, potwierdzenia, logi na żywo, kopiowanie. */
(function () {
  "use strict";

  // ---------- Toasty ----------
  document.addEventListener("alpine:init", () => {
    Alpine.store("toasts", {
      items: [],
      push(message, level = "ok") {
        const id = Date.now() + Math.random();
        this.items.push({ id, message, level });
        setTimeout(() => this.remove(id), level === "err" ? 9000 : 4500);
      },
      remove(id) { this.items = this.items.filter((t) => t.id !== id); },
    });

    Alpine.store("confirm", {
      open: false, title: "", text: "", danger: false, math: null, answers: [], resolve: null,
      ask({ title, text, danger = false, math = false }) {
        this.title = title; this.text = text; this.danger = danger; this.math = null; this.answers = [];
        if (math) {
          const a = 20 + Math.floor(Math.random() * 21), b = 20 + Math.floor(Math.random() * 21);
          const op = Math.random() < 0.5 ? "+" : "-";
          const result = op === "+" ? a + b : a - b;
          const set = new Set([result]);
          while (set.size < 6) set.add(result + Math.floor(Math.random() * 31) - 15);
          this.math = { question: `${a} ${op} ${b} = ?`, result };
          this.answers = [...set].sort(() => Math.random() - 0.5);
        }
        this.open = true;
        return new Promise((resolve) => { this.resolve = resolve; });
      },
      answer(ok) { this.open = false; if (this.resolve) this.resolve(ok); this.resolve = null; },
    });
  });

  // ---------- Menedżer plików kontenera (edytor w modalu) ----------
  document.addEventListener("alpine:init", () => {
    Alpine.data("containerFiles", (urls) => {
      const q = (path) => new URLSearchParams(urls.ep === "local" ? {path} : {path, ep: urls.ep}).toString();
      return {
        showEditor: false, showUpload: false, uploadFolder: false, showMkdir: false, loading: false, saving: false,
        filePath: "", fileContent: "", isBinary: false, isTruncated: false, fileSize: 0,
        browser() { return document.getElementById("files-browser"); },
        currentPath() { return new URL(this.browser().dataset.url, location.origin).searchParams.get("path") || "/"; },
        downloadUrl() { return `${urls.download}?${q(this.filePath)}`; },
        sizeText() { const s = this.fileSize; return s > 1048576 ? (s / 1048576).toFixed(2) + " MB" : s > 1024 ? (s / 1024).toFixed(1) + " KB" : s + " B"; },
        async openFile(path) {
          Object.assign(this, {filePath: path, showEditor: true, loading: true, fileContent: ""});
          try {
            const res = await fetch(`${urls.read}?${q(path)}`);
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || res.status);
            Object.assign(this, {fileContent: data.content || "", isBinary: !!data.is_binary, isTruncated: !!data.truncated, fileSize: data.size || 0});
          } catch (err) {
            window.toast(String(err.message || err), "err");
            this.showEditor = false;
          } finally {
            this.loading = false;
          }
        },
        async saveFile() {
          this.saving = true;
          try {
            const res = await fetch(urls.write, {method: "POST", body: new URLSearchParams({path: this.filePath, content: this.fileContent}),
              headers: {"X-CSRF-Token": document.querySelector("meta[name=csrf-token]").content}});
            if (!res.ok) throw new Error(res.status);
            window.toast(I18N.saved || "OK", "ok");
            this.showEditor = false;
            htmx.ajax("GET", this.browser().dataset.url, {target: "#files-browser"});
          } catch (err) {
            window.toast(String(err.message || err), "err");
          } finally {
            this.saving = false;
          }
        },
      };
    });
  });

  // ---------- Lista wyboru w stylu Deployry (makro `pick`) ----------
  document.addEventListener("alpine:init", () => {
    Alpine.data("pick", (initial) => ({
      open: false,
      value: String(initial ?? ""),
      options() { return [...this.$root.querySelectorAll(".pick-opt")]; },
      label() {
        const found = this.options().find((o) => o.dataset.value === this.value);
        return found ? found.dataset.label : this.value;
      },
      choose(el) {
        this.value = el.dataset.value;
        this.open = false;
        this.$nextTick(() => this.$root.querySelector("input[type=hidden]").dispatchEvent(new Event("change", { bubbles: true })));
      },
      move(step) {
        const opts = this.options();
        const index = Math.max(0, Math.min(opts.length - 1, opts.findIndex((o) => o.dataset.value === this.value) + step));
        if (opts[index]) this.value = opts[index].dataset.value;
      },
    }));
  });

  // ---------- Lista aplikacji: wyszukiwarka, filtr stanu, sortowanie (kolejność przez CSS `order`) ----------
  document.addEventListener("alpine:init", () => {
    Alpine.data("appList", (items) => {
      const load = (key, fallback) => { try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; } };
      const save = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* tryb prywatny */ } };
      const STOPPED = ["stopped", "exited", "paused", "created"];
      const RANK = { error: 0, deploying: 1, pending: 1, stopped: 2, paused: 2, running: 3 };
      const byName = (a, b) => a.name.localeCompare(b.name);
      const desc = (key) => (a, b) => (b[key] - a[key]) || byName(a, b);  // brak danych (-1) na końcu
      const SORTS = {
        name: byName,
        status: (a, b) => ((RANK[a.status] ?? 2) - (RANK[b.status] ?? 2)) || byName(a, b),
        node: (a, b) => a.node.localeCompare(b.node) || byName(a, b),
        cpu: desc("cpu"),
        mem: desc("mem"),
        uptime: (a, b) => ((a.uptime < 0) - (b.uptime < 0)) || (a.uptime - b.uptime) || byName(a, b),  // najsłabsze najpierw
      };
      return {
        q: "", status: load("apps.status", ""), sort: load("apps.sort", "name"), ranks: {}, matches: {},
        init() {
          this.$watch("sort", (v) => { save("apps.sort", v); this.recompute(); });
          this.$watch("status", (v) => { save("apps.status", v); this.recompute(); });
          this.$watch("q", () => this.recompute());
          this.recompute();
        },
        recompute() {
          const words = this.q.toLowerCase().split(/\s+/).filter(Boolean);
          const matches = {};
          for (const it of items) {
            const okStatus = !this.status || (this.status === "stopped" ? STOPPED.includes(it.status) : it.status === this.status);
            matches[it.id] = okStatus && words.every((w) => it.text.includes(w));
          }
          const ranks = {};
          [...items].sort(SORTS[this.sort] || byName).forEach((it, i) => { ranks[it.id] = i; });
          this.matches = matches;
          this.ranks = ranks;
        },
        visible(id) { return this.matches[id] !== false; },
        order(id) { return this.ranks[id] || 0; },
        get shown() { return Object.values(this.matches).some(Boolean); },
      };
    });
  });

  window.toast = (message, level = "ok") => {
    if (window.Alpine) Alpine.store("toasts").push(message, level);
  };

  document.body.addEventListener("toast", (e) => toast(e.detail.message, e.detail.level));

  // Komunikat po przeładowaniu strony (ciasteczko dpl_flash)
  document.addEventListener("alpine:initialized", () => {
    const match = document.cookie.match(/(?:^|; )dpl_flash=([^;]*)/);
    if (!match) return;
    document.cookie = "dpl_flash=; Max-Age=0; path=/";
    try {
      const data = JSON.parse(decodeURIComponent(match[1].replace(/^"|"$/g, "")));
      toast(data.m, data.l || "ok");
    } catch (_) { /* ignorujemy uszkodzone ciasteczko */ }
  });

  // ---------- HTMX ----------
  document.addEventListener("htmx:configRequest", (e) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) e.detail.headers["X-CSRF-Token"] = meta.content;
  });

  document.addEventListener("htmx:responseError", (e) => {
    const xhr = e.detail.xhr;
    if (xhr.getResponseHeader("HX-Trigger")) return; // serwer sam wysłał toast
    let message = (I18N.error_status || "Error {status}").replace("{status}", xhr.status);
    try {
      const data = JSON.parse(xhr.responseText);
      if (data.detail) message = typeof data.detail === "string" ? data.detail : data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("\n");
    } catch (_) {
      if (xhr.responseText && xhr.responseText.length < 300) message = xhr.responseText;
    }
    toast(message, "err");
  });

  document.addEventListener("htmx:sendError", () => toast(I18N.offline, "err"));

  // Ładniejsze potwierdzenia zamiast window.confirm
  document.addEventListener("htmx:confirm", (e) => {
    const elt = e.detail.elt;
    const question = elt.getAttribute("hx-confirm");
    if (!question) return;
    e.preventDefault();
    Alpine.store("confirm")
      .ask({
        title: elt.dataset.confirmTitle || I18N.confirm_title,
        text: question,
        danger: elt.hasAttribute("data-danger"),
        math: elt.hasAttribute("data-math"),
      })
      .then((ok) => { if (ok) e.detail.issueRequest(true); });
  });

  // ---------- Logi na żywo ----------
  const MAX_LINES = 3000;
  const follow = new WeakMap();
  document.addEventListener("htmx:sseBeforeMessage", (e) => {
    const box = e.detail.elt.closest(".log") || e.detail.elt;
    if (box.classList.contains("log")) follow.set(box, box.scrollHeight - box.scrollTop - box.clientHeight < 60);
  });
  // dopisywanie linii przez hx-swap-oob (konsola Minecrafta): ten sam limit i przewijanie co przy SSE
  document.addEventListener("htmx:oobBeforeSwap", (e) => {
    const box = e.detail.target;
    if (box && box.classList.contains("log")) follow.set(box, box.scrollHeight - box.scrollTop - box.clientHeight < 60);
  });
  document.addEventListener("htmx:oobAfterSwap", (e) => {
    const box = e.detail.target;
    if (!box || !box.classList.contains("log")) return;
    while (box.childElementCount > MAX_LINES) box.firstElementChild.remove();
    if (follow.get(box) !== false) box.scrollTop = box.scrollHeight;
  });
  document.addEventListener("htmx:sseMessage", (e) => {
    const box = e.detail.elt.closest(".log") || e.detail.elt;
    if (!box.classList.contains("log")) return;
    while (box.childElementCount > MAX_LINES) box.firstElementChild.remove();
    if (follow.get(box) !== false) box.scrollTop = box.scrollHeight;
  });

  // ---------- Kopiowanie ----------
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-copy]");
    if (!el) return;
    const value = el.dataset.copy || el.textContent.trim();
    navigator.clipboard.writeText(value).then(
      () => toast(I18N.copied),
      () => toast(I18N.copy_failed, "err"),
    );
  });

  // ---------- Tabele na telefonie ----------
  // Na wąskim ekranie wiersze tabel układają się w karty; każda komórka dostaje podpis z nagłówka kolumny.
  const labelTables = () => {
    document.querySelectorAll("table.table:not(.keep)").forEach((table) => {
      const heads = [...table.querySelectorAll(":scope > thead th")].map((th) => th.textContent.trim());
      if (!heads.length) return;
      table.querySelectorAll(":scope > tbody > tr").forEach((tr) => {
        let col = 0;
        for (const td of tr.children) {
          if (col > 0 && !td.hasAttribute("data-label")) td.setAttribute("data-label", heads[col] || "");
          col += td.colSpan || 1;
        }
      });
    });
  };
  // zakładki przewijane w bok: aktywna ma być widoczna od razu
  const showActiveTab = () => {
    document.querySelectorAll(".tabs").forEach((tabs) => {
      const active = tabs.querySelector("a.active");
      if (active && tabs.scrollWidth > tabs.clientWidth) tabs.scrollLeft = active.offsetLeft - tabs.offsetLeft - 16;
    });
  };
  const mobile = () => { labelTables(); showActiveTab(); };
  document.addEventListener("DOMContentLoaded", mobile);
  document.addEventListener("htmx:afterSettle", mobile);

  // ---------- Mapa infrastruktury (force-graph, ładowany dopiero na tej stronie) ----------
  const loadScript = (src) => new Promise((resolve, reject) => {
    const found = document.querySelector(`script[data-src="${src}"]`);
    if (found) return found.dataset.ready ? resolve() : found.addEventListener("load", resolve);
    const el = Object.assign(document.createElement("script"), { src, onerror: reject });
    el.dataset.src = src;
    el.addEventListener("load", () => { el.dataset.ready = "1"; resolve(); });
    document.head.appendChild(el);
  });
  // znak ikony Bootstrap Icons (z CSS ::before), żeby rysować ją na płótnie tą samą czcionką
  const glyph = (name) => {
    const el = Object.assign(document.createElement("i"), { className: `bi bi-${name}` });
    el.style.position = "absolute"; el.style.visibility = "hidden";
    document.body.appendChild(el);
    const content = getComputedStyle(el, "::before").content.replace(/^"|"$/g, "");
    el.remove();
    return content;
  };
  const MAP_TYPES = {
    panel: { icon: "stars", color: "--violet", size: 9 },
    gateway: { icon: "router", color: "--cyan", size: 7 },
    node: { icon: "hdd-network", color: "--info", size: 7 },
    ct: { icon: "box", color: "--violet-2", size: 5 },
    app: { icon: "rocket-takeoff", color: "--magenta", size: 5.5 },
    site: { icon: "globe2", color: "--text-dim", size: 4 },
  };
  const MAP_STATUS = { running: "--ok", paired: "--ok", ok: "--ok", deploying: "--warn", pending: "--warn", joined: "--warn",
                       new: "--warn", error: "--err", stopped: "--muted", off: "--muted" };
  const LINK_COLORS = { manage: "--violet", tunnel: "--cyan", host: "--border-strong", traffic: "--magenta" };

  document.addEventListener("alpine:init", () => {
    Alpine.data("topologyGraph", (url, labels) => {
      const g = {};  // graf i kolory poza reaktywnym stanem Alpine — rysowanie co klatkę nie idzie przez proxy
      return {
      ready: false, error: "", hidden: {}, layout: "free",
      async init() {
        try {
          await Promise.all([loadScript("/static/vendor/force-graph.min.js?v=1.52.0"), document.fonts.load('16px "bootstrap-icons"')]);
        } catch (_) { this.error = "force-graph"; return; }
        const css = getComputedStyle(document.documentElement);
        const v = (name) => css.getPropertyValue(name).trim();
        g.colors = { text: v("--text"), muted: v("--muted"), bg: v("--surface-solid") };
        g.types = Object.fromEntries(Object.entries(MAP_TYPES).map(([k, t]) => [k, { ...t, color: v(t.color), char: glyph(t.icon) }]));
        g.status = Object.fromEntries(Object.entries(MAP_STATUS).map(([k, c]) => [k, v(c)]));
        g.links = Object.fromEntries(Object.entries(LINK_COLORS).map(([k, c]) => [k, v(c)]));
        this.$root.querySelectorAll(".map-chip").forEach((chip) => chip.style.setProperty("--dot", g.types[chip.dataset.type]?.color));
        this.draw();
        await this.load();
      },
      draw() {
        g.hidden = {};
        const box = this.$refs.canvas;
        const tooltip = (n) => {
          const kind = n.type === "gateway" && n.role ? `${labels.type.gateway} · ${labels.role[n.role] || n.role}` : labels.type[n.type];
          const status = labels.status[n.status] || n.status || "";
          const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
          return `<b>${esc(n.label)}</b><br><span>${esc(kind)}${status ? " · " + esc(status) : ""}${n.host ? " · " + esc(n.host) : ""}</span>`;
        };
        g.graph = new ForceGraph(box)
          .backgroundColor("rgba(0,0,0,0)")
          .width(box.clientWidth).height(box.clientHeight)
          .nodeId("id").nodeLabel(tooltip)
          .nodeVal((n) => g.types[n.type].size)
          .nodeVisibility((n) => !g.hidden[n.type])
          .linkVisibility((l) => !g.hidden[l.source.type ?? l.source] && !g.hidden[l.target.type ?? l.target])
          .linkColor((l) => g.links[l.kind] + "99")
          .linkWidth((l) => (l.kind === "tunnel" ? 1.6 : 1))
          .linkLineDash((l) => (l.kind === "tunnel" ? [3, 2] : null))
          .linkDirectionalArrowLength(3).linkDirectionalArrowRelPos(1)
          .linkDirectionalParticles((l) => (l.kind === "tunnel" || l.kind === "traffic" ? 2 : 0))
          .linkDirectionalParticleWidth(2).linkDirectionalParticleSpeed(0.005)
          .linkDirectionalParticleColor((l) => g.links[l.kind])
          .dagLevelDistance(70)
          .onNodeClick((n) => n.url && (window.location.href = n.url))
          .onNodeHover((n) => { box.style.cursor = n && n.url ? "pointer" : null; })
          .nodeCanvasObject((n, ctx, scale) => this.paint(n, ctx, scale))
          .nodePointerAreaPaint((n, color, ctx) => {
            ctx.fillStyle = color;
            ctx.beginPath(); ctx.arc(n.x, n.y, g.types[n.type].size + 2, 0, 2 * Math.PI); ctx.fill();
          })
          .cooldownTicks(120)
          .onEngineStop(() => { if (!g.fitted) { g.fitted = true; this.fit(); } });
        g.graph.d3Force("charge").strength(-140);
        g.observer = new ResizeObserver(() => g.graph.width(box.clientWidth).height(box.clientHeight));
        g.observer.observe(box);
      },
      paint(n, ctx, scale) {
        const t = g.types[n.type];
        const r = t.size;
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
        ctx.fillStyle = g.colors.bg; ctx.fill();
        ctx.lineWidth = Math.max(1.2, 2 / scale);
        ctx.strokeStyle = g.status[n.status] || t.color; ctx.stroke();
        if (n.img && n.img.complete && n.img.naturalWidth) {
          ctx.save(); ctx.beginPath(); ctx.arc(n.x, n.y, r * 0.72, 0, 2 * Math.PI); ctx.clip();
          ctx.drawImage(n.img, n.x - r * 0.72, n.y - r * 0.72, r * 1.44, r * 1.44); ctx.restore();
        } else {
          ctx.font = `${r * 1.05}px "bootstrap-icons"`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillStyle = t.color; ctx.fillText(t.char, n.x, n.y + r * 0.06);
        }
        // podpisy: zawsze dla głównych elementów, dla aplikacji i domen dopiero po przybliżeniu
        if (["panel", "gateway", "node"].includes(n.type) || scale > 1.6) {
          const size = Math.max(10 / scale, 2.4);
          ctx.font = `${n.type === "site" ? "" : "600 "}${size}px ${getComputedStyle(document.body).fontFamily}`;
          ctx.textAlign = "center"; ctx.textBaseline = "top";
          ctx.fillStyle = n.status === "off" ? g.colors.muted : g.colors.text;
          ctx.fillText(n.label, n.x, n.y + r + 2 / scale);
        }
      },
      async load() {
        try {
          const res = await fetch(url, { headers: { Accept: "application/json" } });
          if (!res.ok) throw new Error(res.status);
          const data = await res.json();
          const old = new Map((g.graph.graphData().nodes || []).map((n) => [n.id, n]));
          data.nodes.forEach((n) => {
            const prev = old.get(n.id);
            if (prev) Object.assign(n, { x: prev.x, y: prev.y, vx: prev.vx, vy: prev.vy, img: prev.img });
            else if (n.app_id) { n.img = new Image(); n.img.src = `/apps/${n.app_id}/icon`; }
          });
          g.graph.graphData(data);
          this.ready = true;
        } catch (exc) { this.error = String(exc); }
      },
      toggle(type) {
        this.hidden = { ...this.hidden, [type]: !this.hidden[type] };
        g.hidden = { ...this.hidden };
        g.graph.nodeVisibility(g.graph.nodeVisibility()).linkVisibility(g.graph.linkVisibility());
      },
      setLayout(mode) {
        this.layout = mode;
        g.graph.dagMode(mode === "free" ? null : mode);
        g.graph.d3Force("charge").strength(mode === "free" ? -140 : -60);
        g.fitted = false;
        g.graph.d3ReheatSimulation();
      },
      fit() { g.graph && g.graph.zoomToFit(500, 50); },
      destroy() {
        g.observer && g.observer.disconnect();
        if (g.graph) { g.graph.pauseAnimation(); g.graph._destructor && g.graph._destructor(); }
      },
    };
    });
  });
})();
