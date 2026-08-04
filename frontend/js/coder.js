/* coder.js — SDLC view (Coder mode) with animated timeline and glass design.
 *
 * POST /api/v1/sdlc/stream with body {requirements, project_context?, images?}.
 * SSE events (all discriminated by msg.event):
 *   thread_started   -> {thread_id}            capture thread_id
 *   approval_needed  -> {message, thread_id}   show Approve/Reject box
 *   error            -> {error}                show error
 *   on_chain_start / on_chain_end (LangGraph v2, name = agent node)
 *                      -> timeline entries for planner/coder/tester/reviewer/supervisor
 * Stream end -> GET /api/v1/sdlc/{thread_id}/state -> render final artifacts.
 */
(function (global) {
  "use strict";

  var NODE_LABEL = {
    planner: "规划", coder: "编码", tester: "测试", reviewer: "审查", supervisor: "调度",
  };
  var TRACKED = ["planner", "coder", "tester", "reviewer", "supervisor"];
  // Status icons
  var STATUS_ICON = {
    success: "✓",
    error: "✕",
    approval: "⏳",
  };

  function Coder() {
    this.threadId = null;
    this.abortCtrl = null;
    this.streamEnded = false;
    this.activeNodes = {};   // track running nodes
  }

  Coder.prototype.mount = function (container) {
    this.container = container;
    container.innerHTML =
      '<div class="coder-wrap">' +
        '<div class="coder-input">' +
          '<form id="sdlc-form">' +
            '<textarea id="sdlc-req" placeholder="描述你要开发的软件需求，例如：写一个快速排序并附带单元测试"></textarea>' +
            '<div class="coder-row">' +
              '<span style="color:var(--text-dim);font-size:12px">提交后进入多 agent 流程：规划 → 编码 → 测试 → 审查 → 人工审批</span>' +
              '<label style="font-size:12px;color:var(--text-dim)">Coder 模型: ' +
                '<select id="sdlc-model" style="margin-left:4px;max-width:200px"></select>' +
              '</label>' +
              '<button type="submit" class="btn-primary">开始</button>' +
            '</div>' +
          '</form>' +
        '</div>' +
        '<div class="timeline" id="sdlc-timeline">' +
          '<div class="timeline-inner" id="sdlc-inner"></div>' +
        '</div>' +
      '</div>';
    this.inner = container.querySelector("#sdlc-inner");
    this.form = container.querySelector("#sdlc-form");
    this.reqInput = container.querySelector("#sdlc-req");
    this.submitBtn = container.querySelector("#sdlc-form button[type=submit]");
    this.modelSelect = container.querySelector("#sdlc-model");
    this.loadCoderModels();

    var self = this;
    this.form.addEventListener("submit", function (e) {
      e.preventDefault();
      var req = self.reqInput.value.trim();
      if (!req) return;
      self.start(req);
    });
  };

  Coder.prototype.unmount = function () {
    if (this.abortCtrl) this.abortCtrl.abort();
  };

  /* Populate #sdlc-model from GET /sdlc/models. Non-fatal on failure. */
  Coder.prototype.loadCoderModels = function () {
    var sel = this.modelSelect;
    if (!sel) return;
    var self = this;
    sel.disabled = true;
    API.getCoderModels().then(function (data) {
      var models = (data && data.models) || [];
      var def = (data && data["default"]) || "";
      sel.innerHTML = "";
      models.forEach(function (name) {
        var opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        if (name === def) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.disabled = false;
    })["catch"](function (err) {
      sel.innerHTML = '<option value="">(模型列表获取失败)</option>';
      sel.disabled = false;
      self.addEvent("error", "模型列表加载失败", err.message);
    });
  };

  Coder.prototype.start = function (requirements) {
    this.threadId = null;
    this.streamEnded = false;
    this.activeNodes = {};
    this.inner.innerHTML = "";
    this.submitBtn.disabled = true;
    this.submitBtn.textContent = "运行中…";
    var self = this;
    this.abortCtrl = new AbortController();
    var payload = { requirements: requirements };
    var sel = (this.modelSelect && this.modelSelect.value) || "";
    if (sel) payload.coder_model = sel;
    API.streamSSE("/sdlc/stream", payload, {
      signal: this.abortCtrl.signal,
      onMessage: function (m) { self.handleEvent(m); },
      onError: function (msg) {
        self.addEvent("error", "流式错误", msg);
        self.finish();
      },
    }).then(function () {
      if (!self.streamEnded) self.fetchState();
    });
  };

  Coder.prototype.handleEvent = function (m) {
    var ev = m.event;
    if (ev === "thread_started") {
      this.threadId = (m.data && m.data.thread_id) || null;
      this.addEvent("success", "任务已启动", "thread_id: " + this.threadId);
      return;
    }
    if (ev === "approval_needed") {
      this.streamEnded = true;
      var msg = (m.data && m.data.message) || "等待人工审批";
      this.addEvent("approval", "需要人工审批", msg);
      this.renderApproval(msg);
      return;
    }
    if (ev === "error") {
      this.addEvent("error", "错误", (m.data && m.data.error) || "未知错误");
      this.streamEnded = true;
      this.finish();
      return;
    }
    // LangGraph v2 chain events for tracked agent nodes
    var name = m.name || "";
    if (TRACKED.indexOf(name) === -1) return;

    if (ev === "on_chain_start") {
      this.activeNodes[name] = true;
      this.addEvent("", "⟳ 开始 " + (NODE_LABEL[name] || name), "");
    } else if (ev === "on_chain_end") {
      this.activeNodes[name] = false;
      var detail = "";
      var d = m.data;
      if (d && typeof d === "object" && d.output && typeof d.output === "object") {
        if (d.output.summary) detail = String(d.output.summary).slice(0, 300);
        else if (d.output.message) detail = String(d.output.message).slice(0, 300);
      }
      this.addEvent("success", "✓ 完成 " + (NODE_LABEL[name] || name), detail);
    }
  };

  /* ---- Animated timeline event ---- */
  Coder.prototype.addEvent = function (cls, title, detail) {
    var row = document.createElement("div");
    row.className = "tl-event " + (cls || "");
    var now = new Date().toLocaleTimeString();

    var dotHtml = '<span class="tl-dot">•</span>';
    if (cls === "success") dotHtml = '<span class="tl-dot">✓</span>';
    else if (cls === "error") dotHtml = '<span class="tl-dot">✕</span>';
    else if (cls === "approval") dotHtml = '<span class="tl-dot">⏳</span>';

    row.innerHTML =
      dotHtml +
      '<div class="tl-body">' +
        '<div class="tl-title"></div>' +
        (detail ? '<div class="tl-detail"></div>' : "") +
      "</div>" +
      '<div class="tl-when">' + now + "</div>";

    row.querySelector(".tl-title").textContent = title;
    if (detail) {
      var det = row.querySelector(".tl-detail");
      if (detail.indexOf("\n") !== -1) {
        var pre = document.createElement("pre");
        pre.textContent = detail;
        det.appendChild(pre);
      } else {
        det.textContent = detail;
      }
    }
    this.inner.appendChild(row);

    var tl = this.container.querySelector("#sdlc-timeline");
    tl.scrollTop = tl.scrollHeight;
  };

  /* ---- Approval box with glass design ---- */
  Coder.prototype.renderApproval = function (message) {
    var self = this;
    var box = document.createElement("div");
    box.className = "approval-box";
    box.innerHTML =
      '<p>⛔ ' + escapeText(message) + '</p>' +
      '<div class="approval-actions">' +
        '<button type="button" class="btn-approve">审批通过</button>' +
        '<input type="text" class="reject-reason" placeholder="驳回理由（可选）" />' +
        '<button type="button" class="btn-reject">驳回</button>' +
      '</div>';

    box.querySelector(".btn-approve").addEventListener("click", function () {
      box.remove();
      self.addEvent("", "已审批，继续执行", "");
      self.streamEnded = false;
      API.approve(self.threadId).then(function (resp) {
        self.renderFinal(resp);
        self.finish();
      })["catch"](function (err) {
        self.addEvent("error", "审批失败", err.message);
        self.finish();
      });
    });

    box.querySelector(".btn-reject").addEventListener("click", function () {
      var reason = box.querySelector(".reject-reason").value.trim() || "(无理由)";
      box.remove();
      self.addEvent("", "已驳回: " + reason, "");
      self.streamEnded = true;
      API.reject(self.threadId, reason).then(function (resp) {
        self.renderFinal(resp);
        self.finish();
      })["catch"](function (err) {
        self.addEvent("error", "驳回失败", err.message);
        self.finish();
      });
    });

    this.inner.appendChild(box);
    var tl = this.container.querySelector("#sdlc-timeline");
    tl.scrollTop = tl.scrollHeight;
  };

  /* ---- Fetch final state ---- */
  Coder.prototype.fetchState = function () {
    var self = this;
    if (!this.threadId) { this.finish(); return; }
    API.getSdlcState(this.threadId).then(function (state) {
      self.renderFinal(state);
      self.finish();
    })["catch"](function (err) {
      self.addEvent("error", "获取结果失败", err.message);
      self.finish();
    });
  };

  /* ---- Render final artifacts ---- */
  Coder.prototype.renderFinal = function (state) {
    if (!state || typeof state !== "object") return;
    var box = document.createElement("div");
    box.className = "final-box";
    var html = "";

    if (Array.isArray(state.code_changes) && state.code_changes.length) {
      html += '<div class="final-section"><h3>📝 代码变更</h3>';
      state.code_changes.forEach(function (c) {
        html += '<div class="file"><div class="file-path">📄 ' + escapeText(c.file_path) +
          ' <span style="color:var(--text-muted);font-size:11px">(' + escapeText(c.change_type || "?") + ")</span></div>";
        if (c.content) html += "<pre>" + escapeText(c.content) + "</pre>";
        html += "</div>";
      });
      html += "</div>";
    }

    if (Array.isArray(state.test_results) && state.test_results.length) {
      html += '<div class="final-section"><h3>🧪 测试结果</h3>';
      state.test_results.forEach(function (t) {
        var cls = (t.status === "pass") ? "pass" : (t.status === "fail" ? "fail" : "skip");
        html += '<div style="margin-bottom:6px"><span class="badge ' + cls + '">' + escapeText(t.status) + "</span> " +
          escapeText(t.test_name) + (t.error_message ? ' <span style="color:var(--red);font-size:12px">— ' + escapeText(t.error_message) + "</span>" : "") + "</div>";
      });
      html += "</div>";
    }

    if (Array.isArray(state.plan) && state.plan.length) {
      html += '<div class="final-section"><h3>📋 执行计划</h3><ol>';
      state.plan.forEach(function (p) {
        html += "<li>" + escapeText(p.description || p.id || "") + "</li>";
      });
      html += "</ol></div>";
    }

    if (Array.isArray(state.feedback) && state.feedback.length) {
      html += '<div class="final-section"><h3>🔍 审查反馈</h3>';
      state.feedback.forEach(function (f) {
        html += '<div style="margin-bottom:6px"><strong>' + escapeText(f.agent_name) + "</strong> " +
          (f.severity ? '<span class="badge ' + (f.severity === "error" ? "fail" : "pass") + '" style="margin:0 4px">' + escapeText(f.severity) + "</span>" : "") +
          escapeText(f.content) + "</div>";
      });
      html += "</div>";
    }

    if (state.message) {
      html += '<div class="final-section"><h3>📄 总结</h3><p style="margin:0">' + escapeText(state.message) + "</p></div>";
    }

    if (html) {
      box.innerHTML = html;
      this.inner.appendChild(box);
      var tl = this.container.querySelector("#sdlc-timeline");
      tl.scrollTop = tl.scrollHeight;
    }
    this.renderModelExecutions();
  };

  /* Render content-free server-side audit records after a task completes. */
  Coder.prototype.renderModelExecutions = function () {
    var self = this;
    if (!this.threadId) return;
    API.getModelExecutions(this.threadId).then(function (data) {
      var items = (data && data.executions) || [];
      items.forEach(function (item) {
        var status = item.outcome === "succeeded" ? "success" : "error";
        var title = "模型 " + (item.outcome === "succeeded" ? "完成" : "失败") +
          " · " + (item.actual_model || item.effective_model || "未知模型");
        var detail = (item.agent_name || "unknown") + " / " + (item.purpose || "general") +
          " · " + (item.deployment || "unknown") + " · 第 " + (item.attempt || 1) +
          " 次 · " + (item.duration_ms || 0) + "ms";
        if (item.error_type) detail += " · " + item.error_type;
        self.addEvent(status, title, detail);
      });
    })["catch"](function () {
      /* Audit visibility is non-critical; workflow output stays usable. */
    });
  };

  Coder.prototype.finish = function () {
    this.submitBtn.disabled = false;
    this.submitBtn.textContent = "开始";
  };

  function escapeText(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  global.Coder = Coder;
})(window);
