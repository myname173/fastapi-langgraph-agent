/* app.js — entry point: routing, view switching, auth gating, mode memory.
 *
 * HTML5 history API routing: /login | /investor | /coder.
 * Mode (investor/coder) is remembered in localStorage and restored on reload.
 * The backend (SPAStaticFiles) falls back to index.html for unknown non-API
 * paths, so /investor and /coder work on refresh.
 */
(function (global) {
  "use strict";

  var K_MODE = "sdlc_last_mode";
  var App = {
    current: null,      // current view controller (Investor | Coder)
    currentMode: null,  // "investor" | "coder"
  };

  App.init = function () {
    Auth.onAuthed = function () { App.navigate("/investor"); };  // 登录成功 → push 到 investor
    Auth.init();

    document.getElementById("btn-logout").addEventListener("click", function () {
      API.clearTokens();
      App.navigate("/login");  // push
    });
    document.getElementById("tab-investor").addEventListener("click", function () { App.navigate("/investor"); });
    document.getElementById("tab-coder").addEventListener("click", function () { App.navigate("/coder"); });

    global.addEventListener("popstate", App.route);  // 后退/前进 → 仅渲染，不 push
    App.route();  // 首屏：读当前 pathname 渲染

    // Register service worker for PWA installability (no-op if unsupported).
    if ("serviceWorker" in navigator) {
      global.addEventListener("load", function () {
        navigator.serviceWorker.register("/sw.js").catch(function () { /* silent */ });
      });
    }
  };

  /* 用户主动导航：pushState 一条历史，再渲染。 */
  App.navigate = function (path) {
    if (location.pathname !== path) history.pushState({}, "", path);
    App.route();
  };

  /* 仅渲染当前 pathname，不改历史（popstate / 首屏调用）。绝不 pushState。 */
  App.route = function () {
    var path = location.pathname;
    if (!API.getSessionToken()) {
      App.showLogin(path !== "/login");  // 不在 /login 则 replaceState 归一化
      return;
    }
    var mode = (path === "/coder") ? "coder" : (path === "/investor" ? "investor" : (localStorage.getItem(K_MODE) || "investor"));
    // 已登录但路径不是合法 mode 页 → replaceState 归一化（不产生新历史）
    if (path !== "/investor" && path !== "/coder") {
      history.replaceState({}, "", "/" + mode);
    }
    App.enterMain(mode);
  };

  App.showLogin = function (normalize) {
    document.getElementById("view-login").classList.remove("hidden");
    document.getElementById("view-main").classList.add("hidden");
    if (normalize) history.replaceState({}, "", "/login");
    if (App.current && App.current.unmount) App.current.unmount();
    App.current = null;
  };

  App.enterMain = function (mode) {
    document.getElementById("view-login").classList.add("hidden");
    document.getElementById("view-main").classList.remove("hidden");
    if (!mode) mode = localStorage.getItem(K_MODE) || "investor";
    App.switchMode(mode);
  };

  App.switchMode = function (mode) {
    if (App.currentMode === mode && App.current) return;
    if (App.current && App.current.unmount) App.current.unmount();
    App.currentMode = mode;
    localStorage.setItem(K_MODE, mode);
    // URL 由 navigate() 负责；这里不 push，避免重复历史。
    document.getElementById("tab-investor").classList.toggle("active", mode === "investor");
    document.getElementById("tab-coder").classList.toggle("active", mode === "coder");

    var content = document.getElementById("content");
    content.innerHTML = "";
    if (mode === "investor") {
      App.current = new Investor();
    } else {
      App.current = new Coder();
    }
    App.current.mount(content);
  };

  // boot
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", App.init);
  } else {
    App.init();
  }

  global.App = App;
})(window);
