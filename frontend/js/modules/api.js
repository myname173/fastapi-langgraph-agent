/* api.js — ES Module API client.
 *
 * Wraps the FastAPI backend (same origin, prefix /api/v1). Handles the
 * two-step token flow (user token -> session token) and SSE streaming via
 * fetch + ReadableStream (EventSource cannot do POST + Authorization header).
 *
 * All backend endpoints are covered here so every backend feature is exposed
 * to the UI: auth/sessions, chat, SDLC, watchlist, portfolio, risk-profile,
 * alerts, goals, media, feature-flags, model-executions.
 */

const API_BASE = "/api/v1";
const K_USER = "sdlc_user_token";
const K_SESSION = "sdlc_session_token";

function getUserToken() { return localStorage.getItem(K_USER) || ""; }
function setUserToken(t) { localStorage.setItem(K_USER, t); }
function getSessionToken() { return localStorage.getItem(K_SESSION) || ""; }
function setSessionToken(t) { localStorage.setItem(K_SESSION, t); }
function clearTokens() { localStorage.removeItem(K_USER); localStorage.removeItem(K_SESSION); }

function onUnauthorized() {
  clearTokens();
  if (location.hash !== "#/login") location.hash = "#/login";
}

async function parseError(text, status) {
  try {
    const j = JSON.parse(text);
    if (j.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    if (j.errors) return (j.errors || []).map((e) => e.message).join("; ");
    if (j.message) return j.message;
  } catch { /* not json */ }
  return `请求失败 (HTTP ${status})`;
}

async function _request(path, options = {}, tokenType = "session") {
  const token = tokenType === "session" ? getSessionToken() : getUserToken();
  const headers = Object.assign({}, options.headers || {});
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(API_BASE + path, Object.assign({}, options, { headers }));
  if (resp.status === 401) { onUnauthorized(); throw new Error("未授权，请重新登录"); }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(await parseError(text, resp.status));
  }
  if (resp.status === 204) return null;
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) return resp.json();
  return resp.text();
}

/* ---------- Auth ---------- */

async function register(email, password, username) {
  const body = { email, password };
  if (username) body.username = username;
  const data = await fetch(API_BASE + "/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(async (r) => {
    if (!r.ok) throw new Error(await parseError(await r.text(), r.status));
    return r.json();
  });
  const tok = data?.token?.access_token;
  if (!tok) throw new Error("注册响应缺少 token");
  setUserToken(tok);
  return { token: tok, user: data };
}

async function login(email, password) {
  const fd = new FormData();
  fd.append("email", email);
  fd.append("password", password);
  fd.append("grant_type", "password");
  const data = await fetch(API_BASE + "/auth/login", { method: "POST", body: fd })
    .then(async (r) => {
      if (!r.ok) throw new Error(await parseError(await r.text(), r.status));
      return r.json();
    });
  if (!data.access_token) throw new Error("登录响应缺少 access_token");
  setUserToken(data.access_token);
  return { token: data.access_token };
}

async function createSession() {
  const data = await _request("/auth/session", { method: "POST" }, "user");
  const tok = data?.token?.access_token;
  if (!tok) throw new Error("会话响应缺少 token");
  setSessionToken(tok);
  return data;
}

async function listSessions() {
  return _request("/auth/sessions", {}, "user");
}

async function resumeSession(sessionId) {
  const data = await _request(`/auth/session/${encodeURIComponent(sessionId)}/resume`, { method: "POST" }, "user");
  const tok = data?.token?.access_token;
  if (!tok) throw new Error("恢复会话响应缺少 token");
  setSessionToken(tok);
  return data;
}

async function renameSession(sessionId, name) {
  const fd = new FormData();
  fd.append("name", name);
  return _request(`/auth/session/${encodeURIComponent(sessionId)}/name`, { method: "PATCH", body: fd }, "user");
}

async function deleteSession(sessionId) {
  await _request(`/auth/session/${encodeURIComponent(sessionId)}`, { method: "DELETE" }, "user");
  return true;
}

async function getSessionMessages() {
  return _request("/chatbot/messages");
}

async function clearChatHistory() {
  await _request("/chatbot/messages", { method: "DELETE" });
  return true;
}

/* ---------- Chat ---------- */

async function chatNonStream(messages, images) {
  const body = { messages };
  if (images && images.length) body.images = images;
  return _request("/chatbot/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

/* SSE chat stream. handlers: {onMessage, onError, onDone, signal} */
function streamChat(messages, images, handlers = {}) {
  const body = { messages };
  if (images && images.length) body.images = images;
  return streamSSE("/chatbot/chat/stream", body, handlers);
}

/* ---------- SDLC ---------- */

async function sdlcOrchestrate(payload) {
  return _request("/sdlc/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function streamSdlc(payload, handlers = {}) {
  return streamSSE("/sdlc/stream", payload, handlers);
}

async function getSdlcState(threadId) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/state`);
}

async function approveTask(threadId) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/approve`, { method: "POST" });
}

async function rejectTask(threadId, reason) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason || "" }),
  });
}

async function replayTask(threadId) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/replay`, { method: "POST" });
}

async function listTasks() {
  return _request("/sdlc/tasks");
}

async function getTask(taskId) {
  return _request(`/sdlc/tasks/${encodeURIComponent(taskId)}`);
}

async function cancelTask(taskId) {
  return _request(`/sdlc/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
}

async function deleteTask(taskId) {
  return _request(`/sdlc/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

async function getTaskApprovals(threadId) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/approvals`);
}

async function getTaskArtifacts(threadId) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/artifacts`);
}

async function getModelExecutions(threadId) {
  return _request(`/sdlc/${encodeURIComponent(threadId)}/model-executions`);
}

async function getCoderModels() {
  return _request("/sdlc/models");
}

async function getFeatureFlags() {
  return _request("/sdlc/feature-flags");
}

/* ---------- Watchlist ---------- */

async function listWatchlist() {
  return _request("/watchlist");
}

async function addWatchlist(symbol, name, notes) {
  const q = new URLSearchParams({ symbol, name: name || "", notes: notes || "" }).toString();
  return _request(`/watchlist?${q}`, { method: "POST" });
}

async function removeWatchlist(itemId) {
  return _request(`/watchlist/${encodeURIComponent(itemId)}`, { method: "DELETE" });
}

/* ---------- Portfolio ---------- */

async function listPortfolios() {
  return _request("/portfolio");
}

async function createPortfolio(name, riskTolerance, cash) {
  return _request("/portfolio", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, risk_tolerance: riskTolerance || "balanced", cash: cash || 0 }),
  });
}

async function getPortfolio(pid) {
  return _request(`/portfolio/${encodeURIComponent(pid)}`);
}

async function updatePortfolio(pid, body) {
  return _request(`/portfolio/${encodeURIComponent(pid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function deletePortfolio(pid) {
  return _request(`/portfolio/${encodeURIComponent(pid)}`, { method: "DELETE" });
}

async function addHolding(pid, symbol, name, shares, costPrice) {
  return _request(`/portfolio/${encodeURIComponent(pid)}/holdings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, name: name || "", shares, cost_price: costPrice }),
  });
}

async function updateHolding(pid, hid, body) {
  return _request(`/portfolio/${encodeURIComponent(pid)}/holdings/${encodeURIComponent(hid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function deleteHolding(pid, hid) {
  return _request(`/portfolio/${encodeURIComponent(pid)}/holdings/${encodeURIComponent(hid)}`, { method: "DELETE" });
}

async function analyzePortfolio(pid) {
  return _request(`/portfolio/${encodeURIComponent(pid)}/analysis`);
}

/* ---------- Risk Profile ---------- */

async function getRiskProfile() {
  return _request("/risk-profile/me");
}

async function upsertRiskProfile(body) {
  return _request("/risk-profile/me", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/* ---------- Alerts ---------- */

async function listAlerts(activeOnly = false) {
  const q = activeOnly ? "?active_only=true" : "";
  return _request(`/alerts${q}`);
}

async function createAlert(body) {
  return _request("/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function updateAlert(alertId, body) {
  return _request(`/alerts/${encodeURIComponent(alertId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function fireAlert(alertId) {
  return _request(`/alerts/${encodeURIComponent(alertId)}/fire`, { method: "POST" });
}

async function deleteAlert(alertId) {
  return _request(`/alerts/${encodeURIComponent(alertId)}`, { method: "DELETE" });
}

/* ---------- Goals ---------- */

/* SDLC-side only. GET /goals returns the user's SDLC task groups, NOT
 * investment objectives (those live on /risk-profile/me -> investment_goals).
 * Currently unused by the UI — kept for parity with the backend router. */
async function listGoals() {
  return _request("/goals");
}

/* ---------- Media ---------- */

async function listMedia() {
  return _request("/media");
}

/* ---------- SSE ---------- */

function streamSSE(path, body, handlers = {}) {
  const token = getSessionToken();
  if (!token) { onUnauthorized(); return Promise.reject(new Error("无会话 token")); }
  return fetch(API_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body || {}),
    signal: handlers.signal,
  }).then(async (resp) => {
    if (!resp.ok) {
      const text = await resp.text();
      if (resp.status === 401) onUnauthorized();
      throw new Error(await parseError(text, resp.status));
    }
    if (!resp.body || !resp.body.getReader) throw new Error("当前浏览器不支持流式读取");
    return readSSE(resp.body, handlers);
  }).catch((err) => {
    if (err.name === "AbortError") return;
    if (handlers.onError) handlers.onError(err.message || String(err));
  });
}

function readSSE(stream, handlers) {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  function pump() {
    return reader.read().then((chunk) => {
      if (chunk.done) {
        if (handlers.onDone) handlers.onDone();
        return;
      }
      buffer += decoder.decode(chunk.value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let dataLine = "";
        const lines = frame.split("\n");
        for (const l of lines) {
          const t = l.trim();
          if (t.indexOf("data:") === 0) dataLine += t.slice(5).replace(/^\s/, "");
        }
        if (!dataLine) continue;
        try {
          const msg = JSON.parse(dataLine);
          if (handlers.onMessage) handlers.onMessage(msg);
        } catch { /* skip non-JSON */ }
      }
      return pump();
    });
  }
  return pump();
}

export const API = {
  API_BASE,
  getUserToken, setUserToken, getSessionToken, setSessionToken, clearTokens,
  register, login, createSession, listSessions, resumeSession, renameSession, deleteSession,
  getSessionMessages, clearChatHistory,
  chatNonStream, streamChat,
  sdlcOrchestrate, streamSdlc, getSdlcState, approveTask, rejectTask, replayTask,
  listTasks, getTask, cancelTask, deleteTask, getTaskApprovals, getTaskArtifacts,
  getModelExecutions, getCoderModels, getFeatureFlags,
  listWatchlist, addWatchlist, removeWatchlist,
  listPortfolios, createPortfolio, getPortfolio, updatePortfolio, deletePortfolio,
  addHolding, updateHolding, deleteHolding, analyzePortfolio,
  getRiskProfile, upsertRiskProfile,
  listAlerts, createAlert, updateAlert, fireAlert, deleteAlert,
  listGoals, listMedia,
};
