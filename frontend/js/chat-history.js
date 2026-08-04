/* chat-history.js — left-side chat history sidebar.
 *
 * Lists the user's chat sessions (newest-first) and lets them switch back
 * to a previous session to continue chatting, start a new chat, rename, or
 * delete. Mirrors the Sidebar (investor tools) pattern but lives on the LEFT
 * and is scoped to the Investor chat view.
 *
 * Session switching is delegated to the active view controller
 * (App.current.loadSession / newChat), which owns the chat surface and
 * message rendering. ChatHistory only manages the list UI and token refresh.
 *
 * Token model: list/delete/rename use the USER token (API.userFetch);
 * switching to a session calls API.resumeSession which mints a fresh session
 * token for the chosen thread_id, so subsequent chat streams hit the right
 * LangGraph checkpoint.
 */
(function (global) {
  "use strict";

  var K_OPEN = "chat_history_sidebar";

  var ChatHistory = {
    el: null,
    toggleBtn: null,
    visible: false,
    sessions: [],
    currentSessionId: null,
  };

  /* ---- lifecycle ---- */

  ChatHistory.mount = function (parentEl) {
    if (ChatHistory.el) return;
    ChatHistory.el = document.createElement("aside");
    ChatHistory.el.className = "chat-history-sidebar";
    ChatHistory.el.setAttribute("aria-label", "聊天历史");
    ChatHistory.el.setAttribute("role", "complementary");
    parentEl.appendChild(ChatHistory.el);

    // Toggle button — expected in the navbar (#btn-chathistory, added in
    // index.html). Fall back to dynamic insertion if absent.
    ChatHistory.toggleBtn = document.getElementById("btn-chathistory");
    if (!ChatHistory.toggleBtn) {
      var nav = document.querySelector(".switcher");
      if (nav && nav.parentNode) {
        var btn = document.createElement("button");
        btn.id = "btn-chathistory";
        btn.type = "button";
        btn.className = "btn-chathistory";
        btn.setAttribute("aria-label", "聊天历史");
        btn.setAttribute("title", "聊天历史");
        btn.textContent = "💬";
        btn.addEventListener("click", ChatHistory.toggle);
        nav.parentNode.insertBefore(btn, nav);
        ChatHistory.toggleBtn = btn;
      }
    } else {
      ChatHistory.toggleBtn.addEventListener("click", ChatHistory.toggle);
    }

    var stored = localStorage.getItem(K_OPEN) === "true";
    if (stored) ChatHistory.show(false);

    ChatHistory._render();
  };

  ChatHistory.unmount = function () {
    if (ChatHistory.el) { ChatHistory.el.remove(); ChatHistory.el = null; }
    ChatHistory.toggleBtn = null;
    ChatHistory.sessions = [];
    ChatHistory.currentSessionId = null;
    ChatHistory.visible = false;
  };

  /* ---- toggle ---- */

  ChatHistory.toggle = function () {
    ChatHistory.visible ? ChatHistory.hide() : ChatHistory.show(true);
  };

  ChatHistory.show = function (animate) {
    if (!ChatHistory.el) return;
    ChatHistory.visible = true;
    ChatHistory.el.classList.add("is-open");
    if (animate !== false) ChatHistory.el.classList.add("is-animate");
    else ChatHistory.el.classList.remove("is-animate");
    localStorage.setItem(K_OPEN, "true");
    if (ChatHistory.toggleBtn) ChatHistory.toggleBtn.classList.add("active");
  };

  ChatHistory.hide = function () {
    if (!ChatHistory.el) return;
    ChatHistory.visible = false;
    ChatHistory.el.classList.remove("is-open");
    localStorage.setItem(K_OPEN, "false");
    if (ChatHistory.toggleBtn) ChatHistory.toggleBtn.classList.remove("active");
  };

  /* ---- data ---- */

  ChatHistory.load = function () {
    return API.listSessions()
      .then(function (items) {
        ChatHistory.sessions = Array.isArray(items) ? items : [];
        ChatHistory._render();
      })
      .catch(function () { /* silent — sidebar degrades to empty */ });
  };

  ChatHistory.setCurrent = function (sessionId) {
    ChatHistory.currentSessionId = sessionId;
    ChatHistory._render();
  };

  /* ---- render ---- */

  ChatHistory._render = function () {
    if (!ChatHistory.el) return;
    var cur = ChatHistory.currentSessionId;
    var items = ChatHistory.sessions.map(function (s) {
      var active = s.session_id === cur ? " active" : "";
      var name = ChatHistory._esc(s.name || "新对话");
      var time = ChatHistory._fmtTime(s.created_at);
      return (
        '<div class="ch-item' + active + '" data-id="' + ChatHistory._esc(s.session_id) + '">' +
        '  <div class="ch-item-main">' +
        '    <span class="ch-item-name">' + name + '</span>' +
        (time ? '    <span class="ch-item-time">' + time + '</span>' : '') +
        '  </div>' +
        '  <div class="ch-item-actions">' +
        '    <button class="ch-act" data-act="rename" title="重命名" aria-label="重命名">✏️</button>' +
        '    <button class="ch-act" data-act="delete" title="删除" aria-label="删除">🗑</button>' +
        '  </div>' +
        '</div>'
      );
    }).join("");

    ChatHistory.el.innerHTML =
      '<div class="ch-header">' +
      '  <span>聊天历史</span>' +
      '  <button class="ch-new" id="ch-new-btn" title="新建对话">＋ 新对话</button>' +
      '</div>' +
      '<div class="ch-list">' + (items || '<div class="ch-empty">暂无历史会话</div>') + '</div>';

    var newBtn = ChatHistory.el.querySelector("#ch-new-btn");
    if (newBtn) newBtn.addEventListener("click", ChatHistory._onNew);

    var rows = ChatHistory.el.querySelectorAll(".ch-item");
    for (var i = 0; i < rows.length; i++) {
      rows[i].addEventListener("click", ChatHistory._onClick);
    }
    var acts = ChatHistory.el.querySelectorAll(".ch-act");
    for (var j = 0; j < acts.length; j++) {
      acts[j].addEventListener("click", ChatHistory._onAct);
    }
  };

  /* ---- handlers ---- */

  ChatHistory._onClick = function (e) {
    // Ignore clicks on action buttons (handled by _onAct).
    if (e.target.closest(".ch-act")) return;
    var id = this.getAttribute("data-id");
    if (!id || id === ChatHistory.currentSessionId) return;
    var ctrl = global.App && global.App.current;
    if (ctrl && typeof ctrl.loadSession === "function") {
      ctrl.loadSession(id);
    }
  };

  ChatHistory._onNew = function () {
    var ctrl = global.App && global.App.current;
    if (ctrl && typeof ctrl.newChat === "function") {
      ctrl.newChat();
    }
  };

  ChatHistory._onAct = function (e) {
    e.stopPropagation();
    var row = this.closest(".ch-item");
    if (!row) return;
    var id = row.getAttribute("data-id");
    var act = this.getAttribute("data-act");
    if (act === "delete") ChatHistory._onDelete(id);
    else if (act === "rename") ChatHistory._onRename(id);
  };

  ChatHistory._onDelete = function (id) {
    if (!id) return;
    if (!global.confirm("确定删除此会话？删除后无法恢复。")) return;
    API.deleteSession(id)
      .then(function () {
        // If the active session was deleted, start a fresh one.
        if (id === ChatHistory.currentSessionId) {
          var ctrl = global.App && global.App.current;
          if (ctrl && typeof ctrl.newChat === "function") ctrl.newChat();
        }
        return ChatHistory.load();
      })
      .catch(function () { /* silent */ });
  };

  ChatHistory._onRename = function (id) {
    if (!id) return;
    var s = ChatHistory.sessions.filter(function (x) { return x.session_id === id; })[0];
    var cur = s ? (s.name || "新对话") : "";
    var name = global.prompt("会话名称：", cur);
    if (name === null) return;
    name = (name || "").trim();
    if (!name) return;
    API.renameSession(id, name)
      .then(function () { return ChatHistory.load(); })
      .catch(function () { /* silent */ });
  };

  /* ---- helpers ---- */

  ChatHistory._esc = function (s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  };

  ChatHistory._fmtTime = function (iso) {
    if (!iso) return "";
    var t = new Date(iso);
    if (isNaN(t.getTime())) return "";
    var now = new Date();
    var diff = (now.getTime() - t.getTime()) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
    var m = t.getMonth() + 1;
    var d = t.getDate();
    return (t.getFullYear() === now.getFullYear() ? "" : t.getFullYear() + "/") + m + "/" + d;
  };

  global.ChatHistory = ChatHistory;
})(window);
