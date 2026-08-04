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
        .then(function (userToken) { return API.createSession(userToken); })
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
        .then(function (userToken) { return API.createSession(userToken); })
        .then(function () { if (Auth.onAuthed) Auth.onAuthed(); })
        .catch(function (err) { showError(err.message); })
        .then(function () { btn.disabled = false; });
    });
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
