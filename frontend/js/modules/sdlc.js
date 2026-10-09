/* sdlc.js — SDLC Orchestrator view.
 *
 * Two tabs:
 *   1. 运行 / Stream — launch SDLC workflow, visualize pipeline nodes live via
 *      SSE, show timeline of events, render final artifacts (code/tests/feedback),
 *      handle approve/reject, model audit.
 *   2. 任务管理 — list/search/filter/delete/cancel/replay tasks, approvals, artifacts.
 */
import { API } from "./api.js";
import {
  icon, esc, toast, toastErr, toastOk, openModal, confirmModal, renderMarkdown,
  renderDiff, SDLC_NODES, TASK_STATUS_LABEL, formatTime, timeAgo,
  loadingScreen, emptyState,
} from "./ui.js";

export function renderSdlc(container) {
  container.innerHTML = `
    <div class="tabs-bar">
      <button class="tab-btn active" data-tab="run">${icon("play")} 运行开发任务</button>
      <button class="tab-btn" data-tab="tasks">${icon("list")} 任务管理</button>
    </div>
    <div id="sdlc-panel"></div>`;

  const panel = container.querySelector("#sdlc-panel");
  let currentTab = "run";

  container.querySelectorAll(".tab-btn").forEach((t) => t.addEventListener("click", () => {
    currentTab = t.dataset.tab;
    container.querySelectorAll(".tab-btn").forEach((x) => x.classList.toggle("active", x === t));
    if (currentTab === "run") renderRun(panel);
    else renderTasks(panel);
  }));

  renderRun(panel);
}

/* ================= Run / Stream tab ================= */

function renderRun(container) {
  container.innerHTML = `
    <div class="card mb-8">
      <div class="card-title">${icon("code")} 发起多智能体开发任务</div>
      <div class="field">
        <label>需求描述</label>
        <textarea id="sdlc-req" class="textarea" placeholder="描述你要开发的软件需求，例如：写一个快速排序算法并附带单元测试与文档"></textarea>
      </div>
      <div class="grid grid-2">
        <div class="field">
          <label>Coder 模型</label>
          <select id="sdlc-model" class="select"><option value="">加载中…</option></select>
        </div>
        <div class="field">
          <label>项目上下文（可选）</label>
          <input id="sdlc-context" class="input" placeholder="补充背景、约束、已有代码结构…" />
        </div>
      </div>
      <div class="flex gap-6 items-center">
        <button class="btn btn-primary btn-lg" id="btn-start">${icon("play")} 开始运行</button>
        <button class="btn btn-ghost" id="btn-cancel" disabled>${icon("x")} 停止</button>
        <span class="text-sm text-muted">流程：需求 → 规划 → 编码 → 测试 → 审查，含人工审批节点</span>
      </div>
    </div>
    <div class="card mb-8">
      <div class="card-title">${icon("eye")} 流程可视化</div>
      <div id="sdlc-flow" class="sdlc-flow"></div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-title">${icon("clock")} 实时时间线</div>
        <div id="sdlc-timeline" class="timeline" style="max-height:420px;overflow-y:auto"></div>
      </div>
      <div class="card">
        <div class="card-title">${icon("box")} 执行结果</div>
        <div id="sdlc-results" style="max-height:420px;overflow-y:auto"></div>
      </div>
    </div>`;

  const req = container.querySelector("#sdlc-req");
  const context = container.querySelector("#sdlc-context");
  const modelSel = container.querySelector("#sdlc-model");
  const btnStart = container.querySelector("#btn-start");
  const btnCancel = container.querySelector("#btn-cancel");
  const flowEl = container.querySelector("#sdlc-flow");
  const timelineEl = container.querySelector("#sdlc-timeline");
  const resultsEl = container.querySelector("#sdlc-results");

  const state = {
    threadId: null,
    abortCtrl: null,
    streaming: false,
    nodeStatus: {}, // key -> "idle" | "active" | "done" | "error"
    timeline: [],
  };

  initFlow(flowEl, state);
  renderInitialPipeline(flowEl, state);
  resultsEl.appendChild(emptyState("📄", "运行任务后，这里将展示代码变更、测试结果、审查反馈"));

  // Load models
  API.getCoderModels().then((data) => {
    const models = data?.models || [];
    const def = data?.default || "";
    modelSel.innerHTML = "";
    models.forEach((m) => {
      const o = document.createElement("option");
      o.value = m; o.textContent = m + (m === def ? "（默认）" : "");
      modelSel.appendChild(o);
    });
    if (!models.length) modelSel.innerHTML = '<option value="">(无可用模型)</option>';
  }).catch(() => { modelSel.innerHTML = '<option value="">(模型列表获取失败)</option>'; });

  btnStart.addEventListener("click", () => {
    const requirements = req.value.trim();
    if (!requirements) { toastErr("请输入需求描述"); return; }
    startStream(requirements, context.value.trim());
  });

  btnCancel.addEventListener("click", () => {
    if (state.abortCtrl) { state.abortCtrl.abort(); state.abortCtrl = null; }
    setRunning(btnStart, btnCancel, false);
    addTimeline(state, timelineEl, "warn", "已停止", "用户手动终止本次运行");
  });

  /* ---- helpers ---- */
  function setRunning(startBtn, cancelBtn, running) {
    startBtn.disabled = running;
    cancelBtn.disabled = !running;
    startBtn.innerHTML = running
      ? `${icon("play")} 运行中…`
      : `${icon("play")} 开始运行`;
  }

  function startStream(requirements, projectContext) {
    state.streaming = true;
    state.timeline = [];
    state.nodeStatus = {};
    timelineEl.innerHTML = "";
    resultsEl.innerHTML = "";
    renderInitialPipeline(flowEl, state);
    setRunning(btnStart, btnCancel, true);

    state.abortCtrl = new AbortController();
    // Expose so the legacy router can cancel the stream on view unmount.
    window.__sdlcAbort = state.abortCtrl;
    const payload = { requirements };
    if (projectContext) payload.project_context = projectContext;
    const sel = modelSel.value;
    if (sel) payload.coder_model = sel;

    addTimeline(state, timelineEl, "", "任务已提交", "正在初始化多智能体工作流…");

    API.streamSdlc(payload, {
      signal: state.abortCtrl.signal,
      onMessage: (m) => handleEvent(m),
      onError: (msg) => {
        addTimeline(state, timelineEl, "err", "流式错误", msg);
        setRunning(btnStart, btnCancel, false);
      },
      onDone: () => {
        // Stream ended; fetch final state if completed
        if (state.threadId && state.streaming) fetchFinalState();
      },
    });
  }

  function handleEvent(m) {
    const ev = m.event;

    if (ev === "thread_started") {
      state.threadId = m.data?.thread_id || null;
      addTimeline(state, timelineEl, "ok", "任务已启动", `thread_id: ${state.threadId}`);
      return;
    }

    if (ev === "approval_needed") {
      state.streaming = false;
      const msg = m.data?.message || "等待人工审批";
      renderApproval(state, flowEl, timelineEl, resultsEl, msg);
      setRunning(btnStart, btnCancel, false);
      return;
    }

    if (ev === "error") {
      state.streaming = false;
      addTimeline(state, timelineEl, "err", "错误", m.data?.error || "未知错误");
      setRunning(btnStart, btnCancel, false);
      return;
    }

    // LangGraph v2 chain events — map by node name
    const name = m.name || "";
    if (!SDLC_NODES.some((n) => n.key === name)) return;

    if (ev === "on_chain_start") {
      state.nodeStatus[name] = "active";
      updateNode(flowEl, state);
      addTimeline(state, timelineEl, "warn", `⟳ ${name} 节点开始执行`, "");
    } else if (ev === "on_chain_end") {
      state.nodeStatus[name] = "done";
      updateNode(flowEl, state);
      let detail = "";
      const out = m.data?.output;
      if (out && typeof out === "object") {
        if (out.summary) detail = String(out.summary).slice(0, 300);
        else if (out.message) detail = String(out.message).slice(0, 300);
      }
      addTimeline(state, timelineEl, "ok", `✓ ${name} 节点完成`, detail);
    } else if (ev === "on_chain_error") {
      state.nodeStatus[name] = "error";
      updateNode(flowEl, state);
      addTimeline(state, timelineEl, "err", `✕ ${name} 节点出错`, "");
    }
  }

  async function fetchFinalState() {
    if (!state.threadId) { setRunning(btnStart, btnCancel, false); return; }
    addTimeline(state, timelineEl, "", "运行结束", "正在获取最终状态…");
    try {
      const st = await API.getSdlcState(state.threadId);
      renderResults(resultsEl, st);
      if (st.is_complete) {
        state.nodeStatus = { po: "done", planner: "done", coder: "done", tester: "done", reviewer: "done", supervisor: "done" };
        updateNode(flowEl, state);
        addTimeline(state, timelineEl, "ok", "任务已完成", "多智能体工作流全部执行完毕");
        toastOk("开发任务完成");
      }
      // model audit
      renderModelAudit(state, resultsEl);
    } catch (err) {
      addTimeline(state, timelineEl, "err", "获取结果失败", err.message);
    }
    setRunning(btnStart, btnCancel, false);
  }
}

/* Pipeline visualization */
function initFlow(flowEl, state) {
  flowEl.innerHTML = "";
  SDLC_NODES.forEach((n, i) => {
    const node = document.createElement("div");
    node.className = "sdlc-node" + (i > 0 ? " connector" : "");
    node.dataset.node = n.key;
    node.innerHTML = `
      <div class="node-box">
        <div class="node-ico">${n.ico}</div>
        <div class="node-name">${esc(n.label)}</div>
        <div class="node-status" data-status>待命</div>
      </div>`;
    flowEl.appendChild(node);
  });
}

function renderInitialPipeline(flowEl, state) {
  flowEl.querySelectorAll("[data-node]").forEach((n) => {
    n.className = "sdlc-node" + (n !== flowEl.firstElementChild ? " connector" : "");
    n.querySelector("[data-status]").textContent = "待命";
  });
}

function updateNode(flowEl, state) {
  flowEl.querySelectorAll("[data-node]").forEach((n) => {
    const key = n.dataset.node;
    const status = state.nodeStatus[key] || "idle";
    n.className = "sdlc-node" + (n !== flowEl.firstElementChild ? " connector" : "") +
      (status === "active" ? " active" : status === "done" ? " done" : status === "error" ? " error" : "");
    const label = n.querySelector("[data-status]");
    if (status === "active") label.textContent = "执行中…";
    else if (status === "done") label.textContent = "✓ 完成";
    else if (status === "error") label.textContent = "✕ 出错";
    else label.textContent = "待命";
  });
}

function addTimeline(state, timelineEl, cls, title, detail) {
  const item = document.createElement("div");
  item.className = "tl-item" + (cls ? ` ${cls}` : "");
  item.innerHTML = `
    <div class="tl-title">${esc(title)}</div>
    ${detail ? `<div class="tl-detail"><pre>${esc(detail)}</pre></div>` : ""}
    <div class="tl-when">${new Date().toLocaleTimeString()}</div>`;
  timelineEl.appendChild(item);
  timelineEl.scrollTop = timelineEl.scrollHeight;
}

/* Approval box */
function renderApproval(state, flowEl, timelineEl, resultsEl, message) {
  const box = document.createElement("div");
  box.className = "approval-box";
  box.innerHTML = `
    <div class="ap-title">${icon("eye")} 需要人工审批</div>
    <div class="ap-msg">${esc(message)}</div>
    <div class="approval-actions">
      <button class="btn btn-ok" data-a="approve">${icon("check")} 审批通过</button>
      <input class="input reject-reason" placeholder="驳回理由（可选）" />
      <button class="btn btn-err" data-a="reject">${icon("x")} 驳回</button>
    </div>`;
  resultsEl.appendChild(box);
  resultsEl.scrollTop = resultsEl.scrollHeight;

  box.querySelector('[data-a="approve"]').addEventListener("click", async () => {
    box.remove();
    addTimeline(state, timelineEl, "", "已审批，继续执行", "");
    try {
      const resp = await API.approveTask(state.threadId);
      renderResults(resultsEl, resp);
      if (resp.is_complete) {
        state.nodeStatus = { po: "done", planner: "done", coder: "done", tester: "done", reviewer: "done", supervisor: "done" };
        updateNode(flowEl, state);
        toastOk("审批通过，任务完成");
      }
    } catch (err) {
      addTimeline(state, timelineEl, "err", "审批失败", err.message);
    }
  });

  box.querySelector('[data-a="reject"]').addEventListener("click", async () => {
    const reason = box.querySelector(".reject-reason").value.trim() || "(无理由)";
    box.remove();
    addTimeline(state, timelineEl, "warn", `已驳回: ${reason}`, "");
    try {
      const resp = await API.rejectTask(state.threadId, reason);
      renderResults(resultsEl, resp);
      toastWarn("任务已驳回");
    } catch (err) {
      addTimeline(state, timelineEl, "err", "驳回失败", err.message);
    }
  });
}

function toastWarn(m) { toast(m, "warn"); }

/* Render final SDLC results */
function renderResults(resultsEl, state) {
  resultsEl.innerHTML = "";
  if (!state || typeof state !== "object") return;

  // Summary message
  if (state.message) {
    const sec = document.createElement("div");
    sec.className = "mb-8";
    sec.innerHTML = `<div class="card-title">${icon("file")} 总结</div><div class="text-sm text-dim">${renderMarkdown(state.message)}</div>`;
    resultsEl.appendChild(sec);
  }

  // Code changes
  if (Array.isArray(state.code_changes) && state.code_changes.length) {
    const sec = document.createElement("div");
    sec.className = "mb-8";
    sec.innerHTML = `<div class="card-title">${icon("file")} 代码变更 <span class="badge gray">${state.code_changes.length}</span></div>`;
    state.code_changes.forEach((c, i) => {
      const f = document.createElement("div");
      f.className = "card mb-6" + (i === 0 ? "" : "");
      f.style.cssText = "padding:12px;margin-bottom:10px";
      const typeBadge = c.change_type === "create" ? "ok" : c.change_type === "delete" ? "err" : "accent";
      f.innerHTML = `
        <div class="flex justify-between items-center mb-4">
          <span class="mono text-sm text-hi">📄 ${esc(c.file_path)} <span class="badge ${typeBadge}" style="margin-left:6px">${esc(c.change_type || "modify")}</span></span>
          <button class="btn btn-sm btn-ghost" data-copy>${icon("copy", 13)}</button>
        </div>
        <div class="diff-view">${renderDiff(c.content, c.change_type)}</div>
        ${c.commit_message ? `<div class="text-xs text-muted mt-4">💬 ${esc(c.commit_message)}</div>` : ""}`;
      f.querySelector("[data-copy]").addEventListener("click", async () => {
        try { await copy(c.content); toastOk("已复制到剪贴板"); } catch { toastErr("复制失败"); }
      });
      sec.appendChild(f);
    });
    resultsEl.appendChild(sec);
  }

  // Test results
  if (Array.isArray(state.test_results) && state.test_results.length) {
    const sec = document.createElement("div");
    sec.className = "mb-8";
    const passed = state.test_results.filter((t) => t.status === "pass").length;
    sec.innerHTML = `<div class="card-title">${icon("check")} 测试结果 <span class="badge ok">${passed} 通过</span></div>`;
    const list = document.createElement("div");
    state.test_results.forEach((t) => {
      const row = document.createElement("div");
      row.className = "flex items-center gap-6";
      row.style.cssText = "padding:7px 0;border-bottom:1px solid var(--line)";
      row.innerHTML = `
        <span class="badge ${t.status === "pass" ? "ok" : t.status === "fail" ? "err" : "gray"}">${esc(t.status)}</span>
        <span class="text-sm text-hi">${esc(t.test_name)}</span>
        ${t.duration_ms ? `<span class="text-xs text-muted mono">${esc(t.duration_ms)}ms</span>` : ""}
        ${t.error_message ? `<span class="text-xs" style="color:var(--err)">— ${esc(t.error_message)}</span>` : ""}`;
      list.appendChild(row);
    });
    sec.appendChild(list);
    resultsEl.appendChild(sec);
  }

  // Plan
  if (Array.isArray(state.plan) && state.plan.length) {
    const sec = document.createElement("div");
    sec.className = "mb-8";
    sec.innerHTML = `<div class="card-title">${icon("list")} 执行计划</div>`;
    const list = document.createElement("ol");
    list.style.cssText = "padding-left:20px;color:var(--text-dim);font-size:13px";
    state.plan.forEach((p) => {
      const li = document.createElement("li");
      li.style.cssText = "margin-bottom:8px";
      li.innerHTML = `${esc(p.description || p.id || "")} ${p.assignee ? `<span class="badge gray">${esc(p.assignee)}</span>` : ""}`;
      list.appendChild(li);
    });
    sec.appendChild(list);
    resultsEl.appendChild(sec);
  }

  // Feedback
  if (Array.isArray(state.feedback) && state.feedback.length) {
    const sec = document.createElement("div");
    sec.className = "mb-8";
    sec.innerHTML = `<div class="card-title">${icon("eye")} 审查反馈</div>`;
    state.feedback.forEach((f) => {
      const row = document.createElement("div");
      row.className = "card";
      row.style.cssText = "padding:12px;margin-bottom:10px";
      const sev = f.severity === "critical" || f.severity === "high" ? "err" : f.severity === "medium" ? "warn" : "gray";
      row.innerHTML = `
        <div class="flex items-center gap-6 mb-4">
          <strong class="text-sm text-hi">${esc(f.agent_name)}</strong>
          ${f.severity ? `<span class="badge ${sev}">${esc(f.severity)}</span>` : ""}
        </div>
        <div class="text-sm text-dim">${esc(f.content)}</div>`;
      sec.appendChild(row);
    });
    resultsEl.appendChild(sec);
  }

  // Security issues
  if (Array.isArray(state.security_issues) && state.security_issues.length) {
    const sec = document.createElement("div");
    sec.className = "mb-8";
    sec.innerHTML = `<div class="card-title">${icon("shield")} 安全问题 <span class="badge err">${state.security_issues.length}</span></div>`;
    state.security_issues.forEach((s) => {
      const sev = s.severity === "critical" || s.severity === "high" ? "err" : "warn";
      const row = document.createElement("div");
      row.className = "card";
      row.style.cssText = "padding:12px;margin-bottom:10px";
      row.innerHTML = `
        <div class="flex items-center gap-6 mb-4"><span class="badge ${sev}">${esc(s.severity)}</span><span class="mono text-xs text-muted">${esc(s.file || "")}</span></div>
        <div class="text-sm text-dim">${esc(s.message)}</div>
        ${s.match ? `<div class="code-block mt-4"><pre>${esc(s.match)}</pre></div>` : ""}`;
      sec.appendChild(row);
    });
    resultsEl.appendChild(sec);
  }

  if (!resultsEl.children.length) {
    resultsEl.appendChild(emptyState("📄", "暂无执行结果", ""));
  }
}

async function renderModelAudit(state, resultsEl) {
  if (!state.threadId) return;
  try {
    const data = await API.getModelExecutions(state.threadId);
    const items = data?.executions || [];
    if (!items.length) return;
    const sec = document.createElement("div");
    sec.className = "mb-8";
    sec.innerHTML = `<div class="card-title">${icon("shield")} 模型调用审计 <span class="badge gray">${items.length}</span></div>`;
    items.forEach((item) => {
      const ok = item.outcome === "succeeded";
      const row = document.createElement("div");
      row.className = "flex justify-between items-center";
      row.style.cssText = "padding:7px 0;border-bottom:1px solid var(--line)";
      row.innerHTML = `
        <div class="flex items-center gap-6" style="min-width:0">
          <span class="badge ${ok ? "ok" : "err"}">${ok ? "成功" : "失败"}</span>
          <span class="text-sm text-hi mono">${esc(item.actual_model || item.effective_model || "?")}</span>
          <span class="text-xs text-muted">${esc(item.agent_name || "")} / ${esc(item.purpose || "")} / ${esc(item.deployment || "")}</span>
        </div>
        <span class="text-xs text-muted mono">第${esc(item.attempt || 1)}次 · ${esc(item.duration_ms || 0)}ms</span>`;
      sec.appendChild(row);
    });
    resultsEl.appendChild(sec);
  } catch { /* audit non-critical */ }
}

async function copy(text) {
  if (navigator.clipboard) return navigator.clipboard.writeText(text);
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  document.execCommand("copy"); ta.remove();
}

/* ================= Tasks tab ================= */

function renderTasks(container) {
  container.innerHTML = "";
  container.appendChild(loadingScreen("加载任务列表…"));
  loadTasks(container);
}

async function loadTasks(container) {
  let tasks;
  try { tasks = await API.listTasks(); }
  catch (err) { container.innerHTML = ""; container.appendChild(emptyState("⚠️", "加载失败", err.message)); return; }

  container.innerHTML = `
    <div class="card">
      <div class="flex justify-between items-center mb-8" style="flex-wrap:wrap;gap:12px">
        <div class="card-title" style="margin-bottom:0">${icon("list")} 全部任务 <span class="badge gray">${tasks.length}</span></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <input class="input" id="task-search" placeholder="搜索需求…" style="width:220px" />
          <div class="seg" id="task-filter">
            <button class="active" data-f="all">全部</button>
            <button data-f="running">进行中</button>
            <button data-f="completed">已完成</button>
            <button data-f="failed">失败</button>
          </div>
          <button class="btn" id="btn-go-run">${icon("play")} 新建任务</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>需求</th><th>状态</th><th>模型</th><th>创建时间</th><th style="width:190px">操作</th>
            </tr>
          </thead>
          <tbody id="task-tbody"></tbody>
        </table>
      </div>
      <div id="task-empty"></div>
    </div>`;

  container.querySelector("#btn-go-run").addEventListener("click", () => {
    // Switch back to the "run" tab (it lives in the outer renderSdlc panel).
    const runTab = document.querySelector('#sdlc-panel').closest('div')?.querySelector('[data-tab="run"]');
    if (runTab) runTab.click();
  });

  let filter = "all";
  let search = "";

  const searchInput = container.querySelector("#task-search");
  searchInput.addEventListener("input", () => { search = searchInput.value.trim().toLowerCase(); renderRows(); });

  container.querySelectorAll("#task-filter button").forEach((b) => b.addEventListener("click", () => {
    container.querySelectorAll("#task-filter button").forEach((x) => x.classList.toggle("active", x === b));
    filter = b.dataset.f; renderRows();
  }));

  function filtered() {
    return tasks.filter((t) => {
      const running = ["specing","planning","coding","testing","reviewing"].includes(t.status);
      if (filter === "running" && !running) return false;
      if (filter === "completed" && t.status !== "completed") return false;
      if (filter === "failed" && t.status !== "failed") return false;
      if (search && !t.requirements.toLowerCase().includes(search)) return false;
      return true;
    });
  }

  function renderRows() {
    const tbody = container.querySelector("#task-tbody");
    const emptyEl = container.querySelector("#task-empty");
    const rows = filtered();
    tbody.innerHTML = "";
    emptyEl.innerHTML = "";
    if (!rows.length) {
      emptyEl.appendChild(emptyState("📝", "没有符合条件的任务", "换个筛选条件，或新建一个开发任务"));
      return;
    }
    rows.forEach((t) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="max-width:340px">
          <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer" class="text-hi" data-detail="${t.id}" data-thread="${t.thread_id}">${esc(t.requirements.slice(0, 80))}${t.requirements.length > 80 ? "…" : ""}</div>
        </td>
        <td><span class="badge ${esc(t.status)}">${esc(TASK_STATUS_LABEL[t.status] || t.status)}</span></td>
        <td class="mono text-dim">${esc(extractModel(t))}</td>
        <td class="text-xs text-muted">${formatTime(t.created_at)}</td>
        <td>
          <div class="flex gap-4">
            <button class="btn btn-sm btn-ghost" data-detail="${t.id}" data-thread="${t.thread_id}" title="详情">${icon("eye", 14)}</button>
            <button class="btn btn-sm btn-ghost" data-replay="${t.id}" data-thread="${t.thread_id}" title="重跑">${icon("refresh", 14)}</button>
            ${["specing","planning","coding","testing","reviewing"].includes(t.status)
              ? `<button class="btn btn-sm btn-warn" data-cancel="${t.id}" title="取消">${icon("x", 14)}</button>` : ""}
            <button class="btn btn-sm btn-err" data-del="${t.id}" title="删除">${icon("trash", 14)}</button>
          </div>
        </td>`;
      tr.querySelectorAll("[data-detail]").forEach((b) => b.addEventListener("click", () => openTaskModal(t)));
      tr.querySelector("[data-replay]").addEventListener("click", () => doReplay(t));
      if (tr.querySelector("[data-cancel]")) tr.querySelector("[data-cancel]").addEventListener("click", () => doCancel(t));
      tr.querySelector("[data-del]").addEventListener("click", () => doDelete(t));
      tbody.appendChild(tr);
    });
  }

  renderRows();

  async function doReplay(t) {
    const ok = await confirmModal({ title: "重跑任务", message: `将以相同需求重新运行一个任务分支：\n${t.requirements.slice(0, 80)}`, confirmText: "重跑" });
    if (!ok) return;
    try {
      const resp = await API.replayTask(t.thread_id);
      toastOk(`重跑成功，新任务 ${resp.new_thread_id.slice(0, 8)}`);
      loadTasks(container);
    } catch (err) { toastErr(err.message); }
  }

  async function doCancel(t) {
    const ok = await confirmModal({ title: "取消任务", message: "确定要取消这个任务吗？", confirmText: "取消任务", danger: true });
    if (!ok) return;
    try { await API.cancelTask(t.id); toastOk("任务已取消"); loadTasks(container); }
    catch (err) { toastErr(err.message); }
  }

  async function doDelete(t) {
    const ok = await confirmModal({ title: "删除任务", message: `确定删除任务？此操作不可撤销。\n${t.requirements.slice(0, 60)}`, confirmText: "删除", danger: true });
    if (!ok) return;
    try { await API.deleteTask(t.id); toastOk("任务已删除"); loadTasks(container); }
    catch (err) { toastErr(err.message); }
  }
}

function extractModel(task) {
  // Prefer the model the run was actually launched with (sdlc_task.coder_model).
  // Previously this read plan[0].model, but the planner never writes a `model`
  // key into plan items — so the column rendered "-" for every single row.
  if (task && task.coder_model) return task.coder_model;
  // Back-compat: some older records may carry a model on the plan.
  try {
    const p = JSON.parse((task && task.plan) || "[]");
    if (Array.isArray(p) && p[0] && p[0].model) return p[0].model;
  } catch { /* not json */ }
  return "-";
}

async function openTaskModal(task) {
  let approvals = [], artifacts = { artifacts: [] }, execs = { executions: [] };
  const [t, a, ar, e] = await Promise.all([
    API.getTask(task.id).catch(() => task),
    API.getTaskApprovals(task.thread_id).catch(() => []),
    API.getTaskArtifacts(task.thread_id).catch(() => ({ artifacts: [] })),
    API.getModelExecutions(task.thread_id).catch(() => ({ executions: [] })),
  ]);
  approvals = a; artifacts = ar; execs = e;

  const body = document.createElement("div");
  body.className = "flex-col gap-8";
  body.innerHTML = `
    <div>
      <div class="text-sm text-muted mb-4">需求</div>
      <div class="card" style="padding:14px">${esc(t.requirements)}</div>
    </div>
    <div class="flex items-center gap-6">
      <span class="badge ${esc(t.status)}">${esc(TASK_STATUS_LABEL[t.status] || t.status)}</span>
      <span class="text-sm text-muted">创建 ${timeAgo(t.created_at)}</span>
      ${t.completed_at ? `<span class="text-sm text-muted">完成 ${timeAgo(t.completed_at)}</span>` : ""}
    </div>
    ${approvals.length ? `
    <div><div class="text-sm text-muted mb-4">审批记录</div>
      ${approvals.map((a) => `
        <div class="flex gap-6 items-center" style="padding:6px 0">
          <span class="badge ${a.action === "approve" ? "ok" : "err"}">${esc(a.action)}</span>
          <span class="text-sm text-dim">${esc(a.reason || "无理由")}</span>
          <span class="text-xs text-muted">${timeAgo(a.created_at)}</span>
        </div>`).join("")}
    </div>` : ""}
    ${artifacts.artifacts.length ? `
    <div><div class="text-sm text-muted mb-4">制品</div>
      ${artifacts.artifacts.map((af) => `
        <div class="flex items-center gap-6" style="padding:6px 0">
          <span class="mono text-sm text-hi">📄 ${esc(af.filename || af.name || "artifact")}</span>
          ${af.preview_url ? `<button class="btn btn-sm btn-ghost" data-open="${esc(af.absolute_url || af.preview_url)}">预览</button>` : ""}
        </div>`).join("")}
    </div>` : ""}
    ${execs.executions.length ? `
    <div><div class="text-sm text-muted mb-4">模型审计</div>
      ${execs.executions.map((it) => `
        <div class="flex justify-between items-center" style="padding:5px 0;border-bottom:1px solid var(--line)">
          <span class="mono text-sm text-hi">${esc(it.actual_model || it.effective_model || "?")}</span>
          <span class="text-xs text-muted">${esc(it.agent_name || "")} · ${it.outcome === "succeeded" ? "成功" : "失败"} · ${esc(it.duration_ms || 0)}ms</span>
        </div>`).join("")}
    </div>` : ""}`;

  openModal({ title: `任务详情 · ${esc(task.id.slice(0, 8))}`, body, large: true });
  body.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => {
    window.open(b.dataset.open, "_blank", "noopener");
  }));
}
