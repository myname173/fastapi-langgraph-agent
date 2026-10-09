/* coder.js — SDLC view (Coder mode).
 *
 * Enhanced with the modern SDLC Orchestrator UI:
 *   - 6-node pipeline visualization (po/planner/coder/tester/reviewer/supervisor)
 *   - real-time SSE streaming of LangGraph v2 events
 *   - human-in-the-loop approve/reject
 *   - final results rendering (code diff, tests, feedback, security, plan)
 *   - task management (list/search/filter/delete/cancel/replay)
 *   - model audit
 *
 * The heavy lifting lives in the ES Module /js/modules/sdlc.js, loaded
 * dynamically here so the legacy IIFE router (app.js -> new Coder()) keeps
 * working unchanged.
 */
(function (global) {
  "use strict";

  function Coder() {
    this._unmount = null;
  }

  Coder.prototype.mount = function (container) {
    var self = this;
    // Show a minimal loading state while the module loads.
    container.innerHTML =
      '<div class="loading-screen"><div class="spinner"></div><span>正在加载 SDLC 工作台…</span></div>';
    // Dynamic import of the modern SDLC module (ES Modules).
    import("/js/modules/sdlc.js")
      .then(function (mod) {
        if (!mod.renderSdlc) throw new Error("SDLC 模块加载失败");
        self._render = mod.renderSdlc;
        self._render(container);
      })
      .catch(function (err) {
        container.innerHTML =
          '<div class="empty-state"><div class="es-ico">⚠️</div>' +
          '<div class="es-title">SDLC 工作台加载失败</div>' +
          '<div class="es-sub">' + esc(err && err.message ? err.message : "未知错误") + '</div></div>';
      });
  };

  Coder.prototype.unmount = function () {
    // Abort any in-flight streaming when leaving the view.
    if (window.__sdlcAbort && window.__sdlcAbort.abort) window.__sdlcAbort.abort();
  };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  global.Coder = Coder;
})(window);
