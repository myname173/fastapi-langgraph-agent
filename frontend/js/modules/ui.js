/* ui.js — shared UI utilities: toast, modal, icons, markdown, diff, helpers. */

/* ---------- Escape ---------- */
export function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ---------- Toast ---------- */
let toastContainer = null;
function ensureToastContainer() {
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-container";
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

export function toast(message, type = "ok", title = "") {
  const c = ensureToastContainer();
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const ico = type === "err" ? "✕" : type === "warn" ? "!" : "✓";
  el.innerHTML = `<span class="t-ico" style="font-weight:800">${ico}</span><div style="min-width:0">${
    title ? `<div style="font-weight:700;font-size:12.5px;margin-bottom:2px">${esc(title)}</div>` : ""
  }<div>${esc(message)}</div></div>`;
  c.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 260);
  }, 3200);
}

export const toastOk = (m) => toast(m, "ok");
export const toastErr = (m) => toast(m, "err");
export const toastWarn = (m) => toast(m, "warn");

/* ---------- Modal ---------- */
export function openModal({ title, body, onClose, large = false }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal ${large ? "modal-lg" : ""}">
      <div class="modal-title">
        <span>${title}</span>
        <button class="icon-btn modal-close" aria-label="关闭">✕</button>
      </div>
      <div class="modal-body"></div>
    </div>`;
  const close = () => { overlay.remove(); if (onClose) onClose(); };
  overlay.querySelector(".modal-close").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  const bodyEl = overlay.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body;
  else if (body) bodyEl.appendChild(body);
  document.body.appendChild(overlay);
  return { overlay, bodyEl, close };
}

export function confirmModal({ title, message, confirmText = "确认", danger = false }) {
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:16px">
        <p style="color:var(--text-dim);font-size:13.5px">${esc(message)}</p>
        <div style="display:flex;gap:10px;justify-content:flex-end">
          <button class="btn btn-ghost" data-act="cancel">取消</button>
          <button class="btn ${danger ? "btn-err" : "btn-primary"}" data-act="ok">${esc(confirmText)}</button>
        </div>
      </div>`;
    const { overlay } = openModal({ title, body: box, onClose: () => resolve(false) });
    box.querySelector('[data-act="ok"]').addEventListener("click", () => { overlay.remove(); resolve(true); });
    box.querySelector('[data-act="cancel"]').addEventListener("click", () => { overlay.remove(); resolve(false); });
  });
}

/* ---------- Icons (inline SVG) ---------- */
const PATHS = {
  dashboard: `<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>`,
  code: `<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>`,
  chat: `<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>`,
  chart: `<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>`,
  bell: `<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>`,
  stock: `<line x1="3" y1="20" x2="21" y2="4"/><circle cx="7" cy="13" r="2"/><circle cx="12" cy="9" r="2"/><circle cx="17" cy="5" r="2"/>`,
  logout: `<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>`,
  menu: `<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>`,
  play: `<polygon points="5 3 19 12 5 21 5 3"/>`,
  refresh: `<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>`,
  trash: `<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>`,
  edit: `<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>`,
  check: `<polyline points="20 6 9 17 4 12"/>`,
  x: `<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>`,
  plus: `<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>`,
  search: `<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>`,
  eye: `<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>`,
  shield: `<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>`,
  file: `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>`,
  list: `<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>`,
  clock: `<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>`,
  box: `<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>`,
  send: `<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>`,
};

export function icon(name, size = 18) {
  const p = PATHS[name] || PATHS.box;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
}

/* ---------- Markdown (lightweight renderer) ---------- */
export function renderMarkdown(text) {
  if (!text) return "";
  let src = String(text);
  // Code blocks first
  const codeBlocks = [];
  src = src.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    const id = `__CB${codeBlocks.length}__`;
    codeBlocks.push({ lang, code: code.replace(/\n$/, "") });
    return id;
  });
  // Inline code
  src = src.replace(/`([^`]+)`/g, (m, c) => `<code>${esc(c)}</code>`);
  // Headers
  src = src.replace(/^######\s+(.*)$/gm, "<h6>$1</h6>");
  src = src.replace(/^#####\s+(.*)$/gm, "<h5>$1</h5>");
  src = src.replace(/^####\s+(.*)$/gm, "<h4>$1</h4>");
  src = src.replace(/^###\s+(.*)$/gm, "<h3>$1</h3>");
  src = src.replace(/^##\s+(.*)$/gm, "<h2>$1</h2>");
  src = src.replace(/^#\s+(.*)$/gm, "<h1>$1</h1>");
  // Blockquote
  src = src.replace(/^&gt;\s?(.*)$/gm, (m, t) => `<blockquote>${t}</blockquote>`);
  // Bold / italic
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  // Unordered lists
  src = src.replace(/(?:^|\n)((?:[-*+]\s+.*\n?)+)/g, (m, block) => {
    const items = block.trim().split("\n").map((l) => l.replace(/^\s*[-*+]\s+/, ""))
      .map((i) => `<li>${inlineLink(i)}</li>`).join("");
    return `<ul>${items}</ul>`;
  });
  // Ordered lists
  src = src.replace(/(?:^|\n)((?:[0-9]+\.\s+.*\n?)+)/g, (m, block) => {
    const items = block.trim().split("\n").map((l) => l.replace(/^\s*[0-9]+\.\s+/, ""))
      .map((i) => `<li>${inlineLink(i)}</li>`).join("");
    return `<ol>${items}</ol>`;
  });
  // Links
  src = inlineLink(src);
  // Line breaks / paragraphs
  src = src.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => `<p>${l}</p>`).join("");
  // Restore code blocks
  codeBlocks.forEach((cb, i) => {
    const lang = cb.lang ? `<span class="code-lang">${esc(cb.lang)}</span>` : "";
    src = src.replace(`__CB${i}__`, `<div class="code-block">${lang}<pre>${esc(cb.code)}</pre></div>`);
  });
  return src;
}

function inlineLink(s) {
  return s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, url) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(t)}</a>`);
}

/* ---------- Diff rendering ---------- */
export function renderDiff(content, changeType) {
  const lines = String(content || "").split("\n");
  if (changeType === "create") {
    return lines.map((l) => `<div class="diff-line add"><span class="ln">+</span>${esc(l)}</div>`).join("");
  }
  if (changeType === "delete") {
    return lines.map((l) => `<div class="diff-line del"><span class="ln">-</span>${esc(l)}</div>`).join("");
  }
  // modify: try to split +/- markers if present, else neutral
  return lines.map((l) => {
    const t = l.replace(/^[+-]\s?/, "");
    if (/^\+/.test(l)) return `<div class="diff-line add"><span class="ln">+</span>${esc(t)}</div>`;
    if (/^-/.test(l)) return `<div class="diff-line del"><span class="ln">-</span>${esc(t)}</div>`;
    return `<div class="diff-line ctx"><span class="ln"> </span>${esc(t)}</div>`;
  }).join("");
}

/* ---------- Status helpers ---------- */
export const SDLC_NODES = [
  { key: "po", label: "需求", ico: "🎯" },
  { key: "planner", label: "规划", ico: "🗺️" },
  { key: "coder", label: "编码", ico: "💻" },
  { key: "tester", label: "测试", ico: "🧪" },
  { key: "reviewer", label: "审查", ico: "🔍" },
  { key: "supervisor", label: "调度", ico: "🧭" },
];

export const TASK_STATUS_LABEL = {
  specing: "需求分析", planning: "规划中", coding: "编码中", testing: "测试中",
  reviewing: "审查中", completed: "已完成", cancelled: "已取消", paused: "已暂停",
  failed: "失败",
};

export function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} 小时前`;
  const days = Math.floor(hrs / 24);
  return `${days} 天前`;
}

/* ---------- Copy to clipboard ---------- */
export function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
  return Promise.resolve();
}

/* ---------- Element helpers ---------- */
export function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

export function loadingScreen(text = "加载中…") {
  const d = document.createElement("div");
  d.className = "loading-screen";
  d.innerHTML = `<div class="spinner"></div><span>${esc(text)}</span>`;
  return d;
}

export function emptyState(ico, title, sub) {
  const d = document.createElement("div");
  d.className = "empty-state";
  d.innerHTML = `<div class="es-ico">${ico}</div><div class="es-title">${esc(title)}</div>${
    sub ? `<div class="es-sub">${esc(sub)}</div>` : ""}`;
  return d;
}
