/* investor.js — chat view (Investor mode) with streaming typewriter animation,
 *            code syntax highlighting, copy buttons, and financial-card
 *            post-processing (quotes, charts, debate, disclaimer).
 *
 * POST /api/v1/chatbot/chat/stream with body {messages:[{role,content}]}.
 * SSE chunks: text tokens {content:"...", done:false}, tool events
 * {type:"tool", label:"...", done:false}, and {content:"", done:true}.
 * Error case: {content:"<safe msg>", done:true}.
 */
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function Investor() {
    this.messages = [];     // full conversation sent to backend each turn
    this.assistantEl = null;
    this.assistantText = "";
    this.assistantTokens = [];  // for typewriter animation
    this.abortCtrl = null;
    this.tokenQueue = [];
    this.tokenTimer = null;
  }

  Investor.prototype.mount = function (container) {
    this.container = container;
    container.innerHTML =
      '<div class="chat-wrap">' +
        '<div class="chat-toolchip" id="chat-toolchip" role="status" aria-live="polite" hidden>' +
          '<span class="dot" aria-hidden="true"></span><span class="chat-toolchip-text"></span>' +
        '</div>' +
        '<div class="chat-messages" id="chat-msgs"></div>' +
        '<div class="chat-input">' +
          '<form id="chat-form">' +
            '<textarea id="chat-text" placeholder="问行情、查知识库、聊投资……（Enter 发送，Shift+Enter 换行）" rows="1"></textarea>' +
            '<button type="submit" class="btn-primary">发送</button>' +
          '</form>' +
        '</div>' +
      '</div>';
    this.msgsEl = container.querySelector("#chat-msgs");
    this.form = container.querySelector("#chat-form");
    this.input = container.querySelector("#chat-text");
    this.toolchip = container.querySelector("#chat-toolchip");
    this.toolchipText = container.querySelector(".chat-toolchip-text");
    this.renderEmpty();

    // Chat history sidebar (left) — list sessions + restore current thread.
    ChatHistory.mount(container);
    this._setCurrentSessionId();
    ChatHistory.load();
    this.loadCurrentMessages();

    var self = this;
    this.form.addEventListener("submit", function (e) { e.preventDefault(); self.send(); });
    this.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self.send(); }
    });
    this.input.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 150) + "px";
    });

    // ---- P2 sidebar ----
    Sidebar.features.watchlist = true;
    Sidebar.features.portfolio = true;
    Sidebar.features.risk = true;
    Sidebar.features.alert = true;
    Sidebar.features.knowledge = true;
    Sidebar.mount(container);
    Sidebar.initFeatures().then(function () {
      if (Sidebar.el) {
        Sidebar.el.innerHTML = Sidebar._template();
        var wasOpen = localStorage.getItem("investor_sidebar") === "true";
        if (wasOpen) { Sidebar.visible = false; Sidebar.show(false); }
      }
    });
  };

  Investor.prototype.unmount = function () {
    if (this.abortCtrl) this.abortCtrl.abort();
    if (this.tokenTimer) clearTimeout(this.tokenTimer);
    Sidebar.unmount();
    ChatHistory.unmount();
  };

  /* ---- Chat history: restore / switch / create sessions ---- */

  /* Decode the session JWT to get the current session_id (sub claim) and
   * mark it active in the chat-history sidebar. */
  Investor.prototype._setCurrentSessionId = function () {
    try {
      var tok = API.getSessionToken();
      if (!tok) return;
      var part = tok.split(".")[1];
      if (!part) return;
      part = part.replace(/-/g, "+").replace(/_/g, "/");
      var json = decodeURIComponent(
        atob(part).split("").map(function (c) {
          return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
        }).join("")
      );
      var payload = JSON.parse(json);
      if (payload && payload.sub) ChatHistory.setCurrent(payload.sub);
    } catch (e) { /* malformed token — ignore */ }
  };

  /* Load the current session's messages (session token) and render them,
   * reusing the live bubble + financial-card pipeline so history looks
   * identical to streaming output. */
  Investor.prototype.loadCurrentMessages = function () {
    var self = this;
    API.getSessionMessages()
      .then(function (data) {
        var msgs = (data && data.messages) || [];
        self.messages = [];
        self.msgsEl.innerHTML = "";
        if (!msgs.length) { self.renderEmpty(); return; }
        for (var i = 0; i < msgs.length; i++) {
          var m = msgs[i];
          self.messages.push({ role: m.role, content: m.content });
          if (m.role === "user") {
            self.addBubble("user", m.content);
          } else {
            var bubble = self.addBubble("assistant", "");
            self.assistantText = m.content;
            self.assistantEl = bubble;
            self.renderAssistantFinal(true);
          }
        }
        self.assistantEl = null;
        self.assistantText = "";
        self.msgsEl.scrollTop = self.msgsEl.scrollHeight;
      })
      .catch(function () { self.renderEmpty(); });
  };

  /* Switch to a previous session: mint a fresh session token for it, then
   * load its messages. Triggered by the chat-history sidebar. */
  Investor.prototype.loadSession = function (sessionId) {
    if (!sessionId) return;
    var self = this;
    if (this.abortCtrl) this.abortCtrl.abort();
    if (this.tokenTimer) clearTimeout(this.tokenTimer);
    API.resumeSession(sessionId)
      .then(function () {
        ChatHistory.setCurrent(sessionId);
        self.loadCurrentMessages();
      })
      .catch(function () { self.renderEmpty(); });
  };

  /* Start a fresh chat session: create a new session (new thread_id), clear
   * the surface, and refresh the sidebar list. */
  Investor.prototype.newChat = function () {
    var self = this;
    if (this.abortCtrl) this.abortCtrl.abort();
    if (this.tokenTimer) clearTimeout(this.tokenTimer);
    var userToken = API.getUserToken();
    API.createSession(userToken)
      .then(function () {
        self._setCurrentSessionId();
        self.messages = [];
        self.assistantEl = null;
        self.assistantText = "";
        self.msgsEl.innerHTML = "";
        self.renderEmpty();
        return ChatHistory.load();
      })
      .catch(function () { /* silent */ });
  };

  /* ---- Tool progress chip (consumes SSE tool events) ---- */
  Investor.prototype.showToolChip = function (label) {
    if (!this.toolchip) return;
    this.toolchipText.textContent = "正在" + label + "…";
    this.toolchip.hidden = false;
  };

  Investor.prototype.hideToolChip = function () {
    if (this.toolchip) this.toolchip.hidden = true;
  };

  /* ---- Empty state with quick financial hints ---- */
  Investor.prototype.renderEmpty = function () {
    this.msgsEl.innerHTML =
      '<div class="chat-empty">' +
        '<span class="chat-empty-icon">💬</span>' +
        '<h3>开始与 Investor 对话</h3>' +
        '<p>AI 投资助手已就绪：查实时行情、看 K 线、读新闻、做多空辩论</p>' +
        '<div class="chat-empty-hints">' +
          '<span class="chat-empty-hint" role="button" tabindex="0" data-template="查一下 [标的] 的实时行情，最新涨跌多少？">📈 查行情</span>' +
          '<span class="chat-empty-hint" role="button" tabindex="0" data-template="画一下 [标的] 近 30 天的日 K 线">📊 看 K 线</span>' +
          '<span class="chat-empty-hint" role="button" tabindex="0" data-template="最近 [标的] 有什么重要新闻？">📰 读新闻</span>' +
          '<span class="chat-empty-hint" role="button" tabindex="0" data-template="对 [标的] 做多空研究辩论">⚖️ 多空辩论</span>' +
        '</div>' +
      '</div>';

    var self = this;
    var hints = this.msgsEl.querySelectorAll(".chat-empty-hint");
    var PLACEHOLDER = "[标的]";
    for (var i = 0; i < hints.length; i++) {
      hints[i].addEventListener("click", function () {
        var tpl = this.getAttribute("data-template") || "";
        self.input.value = tpl;
        self.input.style.height = "auto";
        self.input.style.height = Math.min(self.input.scrollHeight, 150) + "px";
        self.input.focus();
        // 选中占位符 [标的]，用户直接输入股票/行业即可替换；不自动发送，
        // 等用户确认标的后自行按发送，避免查到非预期的固定股票。
        var ph = tpl.indexOf(PLACEHOLDER);
        if (ph !== -1) {
          self.input.setSelectionRange(ph, ph + PLACEHOLDER.length);
        }
      });
      hints[i].addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          this.click();
        }
      });
    }
  };

  /* ---- Send message ---- */
  Investor.prototype.send = function () {
    var text = this.input.value.trim();
    if (!text) return;
    if (this.msgsEl.querySelector(".chat-empty")) this.msgsEl.innerHTML = "";
    this.input.value = "";
    this.input.style.height = "auto";

    this.hideToolChip();
    this.messages.push({ role: "user", content: text });
    this.addBubble("user", text);
    this.assistantText = "";
    this.assistantTokens = [];
    this.tokenQueue = [];
    this.assistantEl = this.addBubble("assistant", "");
    this.appendCursor();

    var self = this;
    this.abortCtrl = new AbortController();
    API.streamSSE("/chatbot/chat/stream", { messages: this.messages }, {
      signal: this.abortCtrl.signal,
      onMessage: function (m) {
        // Tool-call progress events surface as a chip while the agent works.
        if (m.type === "tool") {
          if (m.label) self.showToolChip(m.label);
          else self.hideToolChip();
          return;
        }
        if (m.done) {
          self.hideToolChip();
          self.removeCursor();
          if (m.content) {
            // done:true with content => sanitized error message
            self.assistantText = m.content;
            self.renderAssistantFinal(false);
            self.assistantEl.classList.add("error");
          } else {
            // Finished successfully — flush remaining tokens then final render
            self.flushTokenQueue(true);
            self.messages.push({ role: "assistant", content: self.assistantText });
          }
          self.assistantEl = null;
        } else if (m.content) {
          self.assistantText += m.content;
          self.enqueueToken(m.content);
        }
      },
      onError: function (msg) {
        self.hideToolChip();
        self.removeCursor();
        if (self.tokenTimer) clearTimeout(self.tokenTimer);
        if (!self.assistantText) {
          self.assistantText = "⚠️ " + msg;
          self.renderAssistantFinal(false);
        }
        self.assistantEl = null;
      },
    });
  };

  /* ---- Token queue for typewriter animation ---- */
  Investor.prototype.enqueueToken = function (chunk) {
    // Split into small character groups for smooth animation
    var chars = chunk.split("");
    for (var i = 0; i < chars.length; i++) {
      this.tokenQueue.push(chars[i]);
    }
    this.processTokenQueue();
  };

  Investor.prototype.processTokenQueue = function () {
    var self = this;
    if (this.tokenTimer) return; // already processing

    var flush = function () {
      if (self.tokenQueue.length === 0) {
        self.tokenTimer = null;
        return;
      }
      // Process up to 3 characters per frame for speed
      var batchSize = Math.min(3, self.tokenQueue.length);
      for (var i = 0; i < batchSize; i++) {
        self.tokenQueue.shift();
      }
      self.renderAssistantIncremental();
      // Auto-scroll
      self.msgsEl.scrollTop = self.msgsEl.scrollHeight;
      self.tokenTimer = setTimeout(flush, 10);
    };
    self.tokenTimer = setTimeout(flush, 10);
  };

  Investor.prototype.flushTokenQueue = function (finalize) {
    if (this.tokenTimer) clearTimeout(this.tokenTimer);
    this.tokenTimer = null;
    this.tokenQueue = [];
    if (finalize) {
      this.renderAssistantFinal(true);
    }
  };

  /* ---- Rendering ---- */
  Investor.prototype.renderAssistantIncremental = function () {
    if (this.assistantEl) {
      // Render markdown incrementally (no financial post-processing mid-stream)
      this.assistantEl.innerHTML = MD.render(this.assistantText);
      this.appendCursor();
      this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
    }
  };

  Investor.prototype.renderAssistantFinal = function (enhance) {
    if (this.assistantEl) {
      this.assistantEl.innerHTML = MD.render(this.assistantText);
      // Enhance code blocks with syntax highlighting and copy buttons
      HL.enhanceCodeBlocks(this.assistantEl);
      // Post-process financial content into structured glass cards
      if (enhance !== false) this.enhanceFinancial(this.assistantEl);
      this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
    }
  };

  /* ---- Financial content post-processing (done stage only) ---- */
  Investor.prototype.enhanceFinancial = function (scope) {
    this.enhanceCharts(scope);
    this.enhanceTables(scope);
    this.enhanceDebate(scope);
    this.addDisclaimer(scope);
  };

  Investor.prototype.enhanceCharts = function (scope) {
    var imgs = scope.querySelectorAll('img[src^="/charts/"]');
    for (var i = 0; i < imgs.length; i++) {
      (function (img) {
        try {
          var alt = img.getAttribute("alt") || "图表";
          var card = document.createElement("div");
          card.className = "fin-chart";
          var head = document.createElement("div");
          head.className = "fin-chart-title";
          var titleSpan = document.createElement("span");
          titleSpan.textContent = alt;
          var srcSpan = document.createElement("span");
          srcSpan.className = "fin-source";
          srcSpan.textContent = "图表 · 数据生成";
          head.appendChild(titleSpan);
          head.appendChild(srcSpan);
          card.appendChild(head);
          // Capture the image's original position BEFORE moving it into the
          // card. Reading img.parentNode after appendChild(img) returns the
          // card itself, which made the old code call card.insertBefore(card,
          // img) -> HierarchyRequestError, leaving the chart detached (i.e.
          // invisible) and aborting the rest of the enhancement pass.
          var parent = img.parentNode;
          var next = img.nextSibling;
          card.appendChild(img);
          img.addEventListener("error", function () {
            if (this.parentNode) {
              var fb = document.createElement("div");
              fb.className = "fin-chart-fallback";
              fb.textContent = "图表加载失败，请稍后重试或查看数据原文。";
              this.parentNode.replaceChild(fb, this);
            }
          });
          if (parent && parent.tagName === "P" && parent.parentNode) {
            parent.parentNode.insertBefore(card, parent);
            parent.parentNode.removeChild(parent);
          } else if (parent) {
            parent.insertBefore(card, next);
          }
        } catch (e) {
          // Never let one chart break the whole message render.
          if (window.console && console.warn) console.warn("enhanceCharts failed", e);
        }
      })(imgs[i]);
    }
  };

  Investor.prototype.enhanceTables = function (scope) {
    var tables = scope.querySelectorAll("table");
    for (var i = 0; i < tables.length; i++) {
      (function (table) {
        var headerCells = table.querySelectorAll("thead th");
        var headerText = "";
        for (var h = 0; h < headerCells.length; h++) headerText += headerCells[h].textContent + "|";
        // Only treat quote/comparison tables as financial cards.
        if (!/代码/.test(headerText) || !/价格/.test(headerText)) return;
        var headers = [];
        for (var h2 = 0; h2 < headerCells.length; h2++) headers.push(headerCells[h2].textContent.trim());
        var rows = table.querySelectorAll("tbody tr");
        var replacement = rows.length <= 1
          ? buildQuoteCard(headers, rows[0])
          : buildCompareCard(headers, rows);
        if (replacement && table.parentNode) {
          table.parentNode.replaceChild(replacement, table);
        }
      })(tables[i]);
    }
  };

  Investor.prototype.enhanceDebate = function (scope) {
    var ROLE_MAP = [
      { re: /看多|买方|乐观/i, cls: "bull", title: "看多视角" },
      { re: /看空|卖方|审慎|悲观/i, cls: "bear", title: "看空视角" },
      { re: /中性|研究员|中立/i, cls: "research", title: "中性视角" },
    ];
    var headings = scope.querySelectorAll("h3, h4");
    var sections = [];
    for (var i = 0; i < headings.length; i++) {
      var h = headings[i];
      var txt = h.textContent;
      var matched = null;
      for (var r = 0; r < ROLE_MAP.length; r++) {
        if (ROLE_MAP[r].re.test(txt)) { matched = ROLE_MAP[r]; break; }
      }
      if (!matched) {
        if (/综合结论|结论/.test(txt)) matched = { cls: "conclusion", title: txt };
        else continue;
      }
      var content = [];
      var n = h.nextSibling;
      while (n) {
        if (n.nodeType === 1 && /^(H1|H2|H3|H4|H5|H6)$/.test(n.tagName)) break;
        content.push(n);
        n = n.nextSibling;
      }
      sections.push({ role: matched, heading: h, content: content });
    }
    var roleSections = sections.filter(function (s) { return s.role.cls !== "conclusion"; });
    if (roleSections.length < 2) return; // not a debate — leave markdown intact
    var card = document.createElement("div");
    card.className = "fin-debate";
    roleSections.forEach(function (s) {
      var col = document.createElement("div");
      col.className = s.role.cls;
      var h4 = document.createElement("h4");
      h4.textContent = s.role.title;
      col.appendChild(h4);
      s.content.forEach(function (node) { col.appendChild(node.cloneNode(true)); });
      card.appendChild(col);
    });
    var concl = sections.filter(function (s) { return s.role.cls === "conclusion"; })[0];
    if (concl) {
      var cdiv = document.createElement("div");
      cdiv.className = "fin-debate-conclusion";
      var ct = document.createElement("strong");
      ct.textContent = concl.heading.textContent + "：";
      cdiv.appendChild(ct);
      concl.content.forEach(function (node) { cdiv.appendChild(node.cloneNode(true)); });
      card.appendChild(cdiv);
    }
    var anchor = roleSections[0].heading;
    var parent = anchor.parentNode;
    parent.insertBefore(card, anchor);
    sections.forEach(function (s) {
      s.content.forEach(function (node) { if (node.parentNode) node.parentNode.removeChild(node); });
      if (s.heading.parentNode) s.heading.parentNode.removeChild(s.heading);
    });
  };

  Investor.prototype.addDisclaimer = function (scope) {
    var hasFin = scope.querySelector(".fin-card, .fin-chart, .fin-debate");
    var text = scope.textContent || "";
    var advice = /买入|卖出|建议|仓位|持有|投资|建仓|减仓|止损|目标价/.test(text);
    if (!hasFin && !advice) return;
    if (scope.querySelector(".fin-disclaimer")) return;
    var d = document.createElement("div");
    d.className = "fin-disclaimer";
    d.textContent = "投资有风险，本内容由 AI 生成，仅供研究参考，不构成任何投资建议。请结合自身风险承受能力独立决策。";
    scope.appendChild(d);
  };

  /* ---- Financial card builders ---- */
  function colIndex(headers, name) {
    for (var i = 0; i < headers.length; i++) {
      if (headers[i].indexOf(name) !== -1) return i;
    }
    return -1;
  }

  function cellText(row, idx) {
    if (idx < 0) return "";
    var cells = row.querySelectorAll("td");
    return cells[idx] ? cells[idx].textContent.trim() : "";
  }

  function changeClass(pct) {
    if (/^-|跌/.test(pct.trim())) return "fin-down";
    if (/^\+|^涨/.test(pct.trim())) return "fin-up";
    return "fin-flat";
  }

  function buildQuoteCard(headers, row) {
    if (!row) return null;
    var nameIdx = colIndex(headers, "名称");
    var codeIdx = colIndex(headers, "代码");
    var priceIdx = colIndex(headers, "价格");
    var chgIdx = colIndex(headers, "涨跌");
    var pctIdx = colIndex(headers, "涨跌幅");
    var highIdx = colIndex(headers, "最高");
    var lowIdx = colIndex(headers, "最低");
    var timeIdx = colIndex(headers, "时间");

    var name = cellText(row, nameIdx) || "—";
    var code = cellText(row, codeIdx);
    var price = cellText(row, priceIdx);
    var chg = cellText(row, chgIdx);
    var pct = cellText(row, pctIdx);
    var cls = changeClass(pct);

    var card = document.createElement("div");
    card.className = "fin-card";

    var head = document.createElement("div");
    head.className = "fin-head";
    var left = document.createElement("div");
    var nameEl = document.createElement("span");
    nameEl.className = "fin-name";
    nameEl.textContent = name;
    left.appendChild(nameEl);
    if (code) {
      var codeEl = document.createElement("span");
      codeEl.className = "fin-code";
      codeEl.textContent = code;
      left.appendChild(codeEl);
    }
    var src = document.createElement("span");
    src.className = "fin-source";
    src.textContent = "行情数据" + (cellText(row, timeIdx) ? " · " + cellText(row, timeIdx) : " · 实时");
    head.appendChild(left);
    head.appendChild(src);
    card.appendChild(head);

    var row1 = document.createElement("div");
    row1.className = "fin-quote-row";
    var l1 = document.createElement("div");
    l1.className = "fin-left";
    var lab = document.createElement("span");
    lab.className = "fin-label";
    lab.textContent = "最新价";
    l1.appendChild(lab);
    var r1 = document.createElement("div");
    r1.className = "fin-right";
    var priceEl = document.createElement("span");
    priceEl.className = "fin-price";
    priceEl.textContent = price || "—";
    var chgEl = document.createElement("span");
    chgEl.className = "fin-change " + cls;
    chgEl.textContent = (chg ? chg + " " : "") + (pct || "");
    r1.appendChild(priceEl);
    r1.appendChild(chgEl);
    row1.appendChild(l1);
    row1.appendChild(r1);
    card.appendChild(row1);

    [["最高", highIdx], ["最低", lowIdx], ["涨跌", chgIdx]].forEach(function (pair) {
      if (pair[1] < 0) return;
      var val = cellText(row, pair[1]);
      if (!val) return;
      var rr = document.createElement("div");
      rr.className = "fin-quote-row";
      var ll = document.createElement("div");
      ll.className = "fin-left";
      var lb = document.createElement("span");
      lb.className = "fin-label";
      lb.textContent = pair[0];
      ll.appendChild(lb);
      var rl = document.createElement("div");
      rl.className = "fin-right";
      var v = document.createElement("span");
      v.className = "fin-price";
      v.style.fontSize = "14px";
      v.textContent = val;
      rl.appendChild(v);
      rr.appendChild(ll);
      rr.appendChild(rl);
      card.appendChild(rr);
    });

    // Decorative trend bar sized by magnitude of pct (capped at 100%).
    var mag = parseFloat(pct.replace(/[^\d.]/g, "")) || 0;
    var p = Math.max(8, Math.min(100, mag * 6));
    var trend = document.createElement("div");
    trend.className = "fin-trend " + (cls === "fin-down" ? "down" : "");
    trend.style.setProperty("--p", p + "%");
    card.appendChild(trend);

    return card;
  }

  function buildCompareCard(headers, rows) {
    var card = document.createElement("div");
    card.className = "fin-card";
    var grid = document.createElement("div");
    grid.className = "fin-compare";
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var name = cellText(row, colIndex(headers, "名称")) || "—";
      var code = cellText(row, colIndex(headers, "代码"));
      var price = cellText(row, colIndex(headers, "价格"));
      var chg = cellText(row, colIndex(headers, "涨跌"));
      var pct = cellText(row, colIndex(headers, "涨跌幅"));
      var cls = changeClass(pct);

      var item = document.createElement("div");
      item.className = "fin-cmp-item";
      var nameEl = document.createElement("div");
      nameEl.className = "fin-cmp-name";
      nameEl.textContent = name;
      if (code) {
        var codeEl = document.createElement("span");
        codeEl.className = "fin-code";
        codeEl.textContent = code;
        nameEl.appendChild(codeEl);
      }
      item.appendChild(nameEl);

      [["最新价", price], ["涨跌", (chg ? chg + " " : "") + pct, cls]].forEach(function (m) {
        var mr = document.createElement("div");
        mr.className = "fin-cmp-metric";
        var k = document.createElement("span");
        k.textContent = m[0];
        var v = document.createElement("b");
        v.textContent = m[1] || "—";
        if (m[2]) v.className = m[2];
        mr.appendChild(k);
        mr.appendChild(v);
        item.appendChild(mr);
      });
      grid.appendChild(item);
    }
    card.appendChild(grid);
    return card;
  }

  /* ---- Bubble helpers ---- */
  Investor.prototype.addBubble = function (role, text) {
    var div = document.createElement("div");
    div.className = "msg " + role;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "user") {
      bubble.textContent = text;
    } else if (text) {
      bubble.innerHTML = MD.render(text);
      // Apply highlighting to code blocks
      HL.enhanceCodeBlocks(bubble);
    }
    div.appendChild(bubble);
    this.msgsEl.appendChild(div);
    this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
    return bubble;
  };

  /* ---- Streaming cursor ---- */
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
