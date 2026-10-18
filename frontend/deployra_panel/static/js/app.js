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
        showEditor: false, showUpload: false, showMkdir: false, loading: false, saving: false,
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
})();
