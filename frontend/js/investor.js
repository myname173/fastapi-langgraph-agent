/* investor.js — chat view (Investor mode).
 *
 * POST /api/v1/chatbot/chat/stream with body {messages:[{role,content}]}.
 * SSE chunks: {content:"...", done:false} repeated, then {content:"", done:true}.
 * Error case: {content:"<safe msg>", done:true}.
 */
(function (global) {
  "use strict";

  function Investor() {
    this.messages = []; // full conversation sent to backend each turn
    this.assistantEl = null;
    this.assistantText = "";
    this.abortCtrl = null;
  }

  Investor.prototype.mount = function (container) {
    this.container = container;
    container.innerHTML =
      '<div class="chat-wrap">' +
        '<div class="chat-messages" id="chat-msgs"></div>' +
        '<div class="chat-input">' +
          '<form id="chat-form">' +
            '<textarea id="chat-text" placeholder="问行情、查知识库、聊投资…（Enter 发送，Shift+Enter 换行）" rows="1"></textarea>' +
            '<button type="submit" class="btn-primary">发送</button>' +
          '</form>' +
        '</div>' +
      '</div>';
    this.msgsEl = container.querySelector("#chat-msgs");
    this.form = container.querySelector("#chat-form");
    this.input = container.querySelector("#chat-text");
    this.renderEmpty();

    var self = this;
    this.form.addEventListener("submit", function (e) { e.preventDefault(); self.send(); });
    this.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self.send(); }
    });
    this.input.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 140) + "px";
    });
  };

  Investor.prototype.unmount = function () {
    if (this.abortCtrl) this.abortCtrl.abort();
  };

  Investor.prototype.renderEmpty = function () {
    this.msgsEl.innerHTML = '<div class="chat-empty">开始与 Investor 助手对话吧 👋</div>';
  };

  Investor.prototype.send = function () {
    var text = this.input.value.trim();
    if (!text) return;
    if (this.msgsEl.querySelector(".chat-empty")) this.msgsEl.innerHTML = "";
    this.input.value = "";
    this.input.style.height = "auto";

    this.messages.push({ role: "user", content: text });
    this.addBubble("user", text);
    this.assistantText = "";
    this.assistantEl = this.addBubble("assistant", "");
    this.appendCursor();

    var self = this;
    this.abortCtrl = new AbortController();
    API.streamSSE("/chatbot/chat/stream", { messages: this.messages }, {
      signal: this.abortCtrl.signal,
      onMessage: function (m) {
        if (m.done) {
          self.removeCursor();
          if (m.content) {
            // done:true with content => sanitized error message
            self.assistantText = m.content;
            self.renderAssistant();
            self.assistantEl.classList.add("error");
          } else {
            self.messages.push({ role: "assistant", content: self.assistantText });
          }
          self.assistantEl = null;
        } else if (m.content) {
          self.assistantText += m.content;
          self.renderAssistant();
        }
      },
      onError: function (msg) {
        self.removeCursor();
        if (!self.assistantText) {
          self.assistantText = "⚠️ " + msg;
          self.renderAssistant();
        }
        self.assistantEl = null;
      },
    });
  };

  Investor.prototype.addBubble = function (role, text) {
    var div = document.createElement("div");
    div.className = "msg " + role;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "user") bubble.textContent = text;
    else bubble.innerHTML = text ? MD.render(text) : "";
    div.appendChild(bubble);
    this.msgsEl.appendChild(div);
    this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
    return bubble;
  };

  Investor.prototype.renderAssistant = function () {
    if (this.assistantEl) {
      this.assistantEl.innerHTML = MD.render(this.assistantText);
      this.appendCursor();
      this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
    }
  };

  Investor.prototype.appendCursor = function () {
    if (!this.assistantEl || this.assistantEl.querySelector(".chat-cursor")) return;
    var c = document.createElement("span");
    c.className = "chat-cursor";
    this.assistantEl.appendChild(c);
  };

  Investor.prototype.removeCursor = function () {
    if (this.assistantEl) {
      var c = this.assistantEl.querySelector(".chat-cursor");
      if (c) c.remove();
    }
  };

  global.Investor = Investor;
})(window);
