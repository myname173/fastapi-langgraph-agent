/* app.js — entry point: routing, view switching, auth gating, mode memory,
 *            theme toggle, motion preference persistence.
 *
 * HTML5 history API routing: /login | /investor | /coder.
 * Mode (investor/coder) is remembered in localStorage and restored on reload.
 * The backend (SPAStaticFiles) falls back to index.html for unknown non-API
 * paths, so /investor and /coder work on refresh.
 */
(function (global) {
  "use strict";

  var K_MODE = "sdlc_last_mode";
  var K_MOTION = "sdlc_reduced_motion";

  var App = {
    current: null,      // current view controller (Investor | Coder)
    currentMode: null,  // "investor" | "coder"
  };

  // ---- Motion preference ----
  App.initMotion = function () {
    var stored = localStorage.getItem(K_MOTION);
    if (stored === "reduced") {
      document.documentElement.classList.add("reduced-motion");
    }
    var btn = document.getElementById("btn-motion");
    if (btn) {
      btn.addEventListener("click", function () {
        var reduced = document.documentElement.classList.toggle("reduced-motion");
        localStorage.setItem(K_MOTION, reduced ? "reduced" : "full");
      });
    }
  };

  // ---- Theme detection ----
  App.initTheme = function () {
    var mql = global.matchMedia("(prefers-color-scheme: dark)");
    // We're always dark theme, but respect system preference for contrast.
    // Set theme-color meta dynamically.
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = "#08090f";
  };

  App.init = function () {
    Auth.onAuthed = function () { App.navigate("/investor"); };
    Auth.init();

    App.initMotion();
    App.initTheme();

    document.getElementById("btn-logout").addEventListener("click", function () {
      API.clearTokens();
      App.navigate("/login");
    });
    document.getElementById("tab-investor").addEventListener("click", function () { App.navigate("/investor"); });
    document.getElementById("tab-coder").addEventListener("click", function () { App.navigate("/coder"); });

    global.addEventListener("popstate", App.route);
    App.route();

    if ("serviceWorker" in navigator) {
      global.addEventListener("load", function () {
        navigator.serviceWorker.register("/sw.js").catch(function () { /* silent */ });
      });
    }
  };

  App.navigate = function (path) {
    if (location.pathname !== path) history.pushState({}, "", path);
    App.route();
  };

  App.route = function () {
    var path = location.pathname;
    if (!API.getSessionToken()) {
      App.showLogin(path !== "/login");
      return;
    }
    var mode = (path === "/coder") ? "coder" : (path === "/investor" ? "investor" : (localStorage.getItem(K_MODE) || "investor"));
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

    document.getElementById("tab-investor").classList.toggle("active", mode === "investor");
    document.getElementById("tab-coder").classList.toggle("active", mode === "coder");

    // Add ARIA attributes
    document.getElementById("tab-investor").setAttribute("aria-selected", mode === "investor");
    document.getElementById("tab-coder").setAttribute("aria-selected", mode === "coder");

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
