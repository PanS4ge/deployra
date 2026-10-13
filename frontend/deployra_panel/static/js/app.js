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
