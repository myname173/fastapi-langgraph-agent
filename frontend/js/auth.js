/* auth.js — login/register views + two-step token flow + micro-interactions.
 *
 * Flow: register (JSON, resp.token.access_token) OR login (FormData,
 * resp.access_token) -> user token -> POST /auth/session (resp.token.access_token)
 * -> session token. On success calls App.onAuthed().
 *
 * Field-name asymmetry is intentional: register/session nest the token under
 * `token.access_token` (Token model); login returns a flat `access_token`.
 */
(function (global) {
  "use strict";

  var Auth = {
    onAuthed: null, // set by App
  };

  Auth.init = function () {
    // Tab switching between login / register forms.
    var tabs = document.querySelectorAll("[data-authtab]");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function () {
        for (var j = 0; j < tabs.length; j++) {
          tabs[j].classList.toggle("active", tabs[j] === this);
          tabs[j].setAttribute("aria-selected", tabs[j] === this);
        }
        var mode = this.getAttribute("data-authtab");
        document.getElementById("form-login").classList.toggle("hidden", mode !== "login");
        document.getElementById("form-register").classList.toggle("hidden", mode !== "register");
        hideError();
      });
    }

    // Attach ripple effect to all primary buttons
    var btns = document.querySelectorAll(".btn-primary");
    for (var k = 0; k < btns.length; k++) {
      btns[k].addEventListener("click", createRipple);
    }

    document.getElementById("form-login").addEventListener("submit", function (e) {
      e.preventDefault();
      hideError();
      var btn = this.querySelector("button[type=submit]");
      btn.disabled = true;
      var email = this.email.value.trim();
      var password = this.password.value;
      API.login(email, password)
        .then(function (userToken) { return Auth._acquireSession(userToken); })
        .then(function () { if (Auth.onAuthed) Auth.onAuthed(); })
        .catch(function (err) { showError(err.message); })
        .then(function () { btn.disabled = false; });
    });

    document.getElementById("form-register").addEventListener("submit", function (e) {
      e.preventDefault();
      hideError();
      var btn = this.querySelector("button[type=submit]");
      btn.disabled = true;
      var username = this.username.value.trim();
      var email = this.email.value.trim();
      var password = this.password.value;
      API.register(email, password, username || null)
        .then(function (userToken) { return Auth._acquireSession(userToken); })
        .then(function () { if (Auth.onAuthed) Auth.onAuthed(); })
        .catch(function (err) { showError(err.message); })
        .then(function () { btn.disabled = false; });
    });
  };

  /* Acquire a session token after auth. Resume the user's most recent
   * conversation (ChatGPT-style) instead of creating a new empty session on
   * every login. The old createSession-on-login behavior spawned a storm of
   * empty "新对话" sessions — one per tunnel-reconnect re-login — and left
   * the user staring at a blank chat surface, unable to see their recent
   * messages (which lived in a previous session).
   *
   * We pick the newest session that has a non-empty name (i.e. has messages,
   * since maybe_name_session names a thread after its first turn); only when
   * no prior conversation exists do we create a new one. listSessions is
   * newest-first (backend orders by created_at DESC), so named[0] is the
   * most recent real conversation. */
  Auth._acquireSession = function (userToken) {
    return API.listSessions()
      .then(function (sessions) {
        var named = (sessions || []).filter(function (s) { return s && s.name; });
        if (named.length > 0) return API.resumeSession(named[0].session_id);
        return API.createSession(userToken);
      })
      .catch(function () { return API.createSession(userToken); }); // network hiccup — fall back to new session
  };

  /* ---- Ripple micro-interaction ---- */
  function createRipple(e) {
    var btn = e.currentTarget;
    // Remove old ripples
    var old = btn.querySelectorAll(".ripple");
    for (var i = 0; i < old.length; i++) old[i].remove();

    var rect = btn.getBoundingClientRect();
    var size = Math.max(rect.width, rect.height);
    var x = e.clientX - rect.left - size / 2;
    var y = e.clientY - rect.top - size / 2;

    var ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.width = size + "px";
    ripple.style.height = size + "px";
    ripple.style.left = x + "px";
    ripple.style.top = y + "px";
    btn.appendChild(ripple);

    ripple.addEventListener("animationend", function () { ripple.remove(); });
  }

  /* ---- Error display with shake animation ---- */
  function showError(msg) {
    var el = document.getElementById("auth-error");
    el.textContent = msg || "操作失败";
    el.classList.remove("hidden");
    // Re-trigger shake animation
    el.style.animation = "none";
    el.offsetHeight; // force reflow
    el.style.animation = "";
  }

  function hideError() {
    document.getElementById("auth-error").classList.add("hidden");
  }

  global.Auth = Auth;
})(window);
