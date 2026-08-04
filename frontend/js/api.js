/* api.js — zero-build API client.
 *
 * Wraps the FastAPI backend (same origin, prefix /api/v1). Handles the
 * two-step token flow (user token -> session token) and SSE streaming via
 * fetch + ReadableStream (EventSource cannot do POST + Authorization header).
 *
 * Verified endpoint contracts (see docs/frontend-spa-plan.md):
 *   POST /api/v1/auth/register    JSON {email,password,username?} -> {token:{access_token,...}}
 *   POST /api/v1/auth/login       FormData {email,password,grant_type} -> {access_token,...}
 *   POST /api/v1/auth/session     Bearer <user token> -> {token:{access_token,...}}
 *   POST /api/v1/chatbot/chat/stream  Bearer <session> JSON {messages:[{role,content}]} -> SSE {content,done}
 *   POST /api/v1/sdlc/stream          Bearer <session> JSON {requirements,...} -> SSE {event,data}
 *   POST /api/v1/sdlc/{id}/approve    Bearer <session> -> SDLCResponse
 *   POST /api/v1/sdlc/{id}/reject     Bearer <session> JSON {reason} -> SDLCResponse
 *   GET  /api/v1/sdlc/{id}/state      Bearer <session> -> dict
 *   GET  /api/v1/sdlc/models          Bearer <session> -> {models:[...], default:str}
 */
(function (global) {
  "use strict";

  var API_BASE = "/api/v1";
  var K_USER = "sdlc_user_token";
  var K_SESSION = "sdlc_session_token";

  function getUserToken() { return localStorage.getItem(K_USER) || ""; }
  function setUserToken(t) { localStorage.setItem(K_USER, t); }
  function getSessionToken() { return localStorage.getItem(K_SESSION) || ""; }
  function setSessionToken(t) { localStorage.setItem(K_SESSION, t); }
  function clearTokens() { localStorage.removeItem(K_USER); localStorage.removeItem(K_SESSION); }

  function redirectToLogin() { clearTokens(); location.hash = "#/login"; }

  /* fetch with session token; on 401 clear tokens and bounce to login. */
  function authFetch(path, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      Authorization: "Bearer " + getSessionToken(),
    });
    return fetch(API_BASE + path, options).then(function (resp) {
      if (resp.status === 401) { redirectToLogin(); throw new Error("未授权，请重新登录"); }
      return resp;
    });
  }

  /* fetch with USER token — for session metadata management (list/resume/
   * delete/rename sessions). Distinct from authFetch which uses the session
   * token for chat endpoints. On 401 clear tokens and bounce to login. */
  function userFetch(path, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      Authorization: "Bearer " + getUserToken(),
    });
    return fetch(API_BASE + path, options).then(function (resp) {
      if (resp.status === 401) { redirectToLogin(); throw new Error("未授权，请重新登录"); }
      return resp;
    });
  }

  /* Register: JSON body -> resp.token.access_token (user token). */
  function register(email, password, username) {
    var body = { email: email, password: password };
    if (username) body.username = username;
    return fetch(API_BASE + "/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
      return r.json();
    }).then(function (data) {
      var tok = data && data.token && data.token.access_token;
      if (!tok) throw new Error("注册响应缺少 token");
      setUserToken(tok);
      return tok;
    });
  }

  /* Login: OAuth2 form-encoded body -> resp.access_token (user token). */
  function login(email, password) {
    var fd = new FormData();
    fd.append("email", email);
    fd.append("password", password);
    fd.append("grant_type", "password");
    return fetch(API_BASE + "/auth/login", {
      method: "POST",
      body: fd, // FormData sets multipart/form-data; FastAPI OAuth2 also accepts urlencoded
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
      return r.json();
    }).then(function (data) {
      var tok = data && data.access_token;
      if (!tok) throw new Error("登录响应缺少 access_token");
      setUserToken(tok);
      return tok;
    });
  }

  /* Exchange user token for a session token: resp.token.access_token. */
  function createSession(userToken) {
    return fetch(API_BASE + "/auth/session", {
      method: "POST",
      headers: { Authorization: "Bearer " + userToken },
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
      return r.json();
    }).then(function (data) {
      var tok = data && data.token && data.token.access_token;
      if (!tok) throw new Error("会话响应缺少 token");
      setSessionToken(tok);
      return tok;
    });
  }

  /* List the user's chat sessions (user token). Returns
   * [{session_id, name, created_at}] newest-first. */
  function listSessions() {
    return userFetch("/auth/sessions").then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
      return r.json();
    });
  }

  /* Resume an existing session: POST /auth/session/{id}/resume (user token)
   * -> {session_id, name, token, created_at}. Stores the fresh session token
   * and returns the full payload so the caller can update UI state. */
  function resumeSession(sessionId) {
    return userFetch("/auth/session/" + encodeURIComponent(sessionId) + "/resume", { method: "POST" })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
        return r.json();
      })
      .then(function (data) {
        var tok = data && data.token && data.token.access_token;
        if (!tok) throw new Error("恢复会话响应缺少 token");
        setSessionToken(tok);
        return data;
      });
  }

  /* Get messages for the CURRENT session (session token): {messages:[...]}. */
  function getSessionMessages() {
    return authFetch("/chatbot/messages").then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
      return r.json();
    });
  }

  /* Delete a session (user token). Returns true on success. */
  function deleteSession(sessionId) {
    return userFetch("/auth/session/" + encodeURIComponent(sessionId), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok && r.status !== 204) {
          return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
        }
        return true;
      });
  }

  /* Rename a session (user token, form-encoded name). Returns the updated
   * {session_id, name, created_at}. */
  function renameSession(sessionId, name) {
    var fd = new FormData();
    fd.append("name", name);
    return userFetch("/auth/session/" + encodeURIComponent(sessionId) + "/name", {
      method: "PATCH",
      body: fd,
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(parseError(t, r.status)); });
      return r.json();
    });
  }

  /* Stream SSE from a POST endpoint. handlers: {onMessage, onError, signal}.
   * Parses `data: <json>\n\n` frames across chunk boundaries. */
  function streamSSE(path, body, handlers) {
    handlers = handlers || {};
    var token = getSessionToken();
    if (!token) { redirectToLogin(); return Promise.reject(new Error("无会话 token")); }
    return fetch(API_BASE + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
      },
      body: JSON.stringify(body || {}),
      signal: handlers.signal,
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.text().then(function (t) {
          if (resp.status === 401) redirectToLogin();
          throw new Error(parseError(t, resp.status));
        });
      }
      if (!resp.body || !resp.body.getReader) {
        // Stream API unavailable (very old browser) — bail gracefully.
        throw new Error("当前浏览器不支持流式读取");
      }
      return readSSE(resp.body, handlers);
    }).catch(function (err) {
      if (err.name === "AbortError") return; // user cancelled
      if (handlers.onError) handlers.onError(err.message || String(err));
    });
  }

  function readSSE(stream, handlers) {
    var reader = stream.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return;
        buffer += decoder.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          var frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          var dataLine = "";
          var lines = frame.split("\n");
          for (var i = 0; i < lines.length; i++) {
            var l = lines[i].trim();
            if (l.indexOf("data:") === 0) dataLine += l.slice(5).replace(/^\s/, "");
          }
          if (!dataLine) continue;
          try {
            var msg = JSON.parse(dataLine);
            if (handlers.onMessage) handlers.onMessage(msg);
          } catch (e) { /* non-JSON frame, skip */ }
        }
        return pump();
      });
    }
    return pump();
  }

  function approve(threadId) {
    return authFetch("/sdlc/" + encodeURIComponent(threadId) + "/approve", { method: "POST" })
      .then(function (r) { return r.json(); });
  }

  function reject(threadId, reason) {
    return authFetch("/sdlc/" + encodeURIComponent(threadId) + "/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason || "" }),
    }).then(function (r) { return r.json(); });
  }

  function getSdlcState(threadId) {
    return authFetch("/sdlc/" + encodeURIComponent(threadId) + "/state")
      .then(function (r) { return r.json(); });
  }

  function getModelExecutions(threadId) {
    return authFetch("/sdlc/" + encodeURIComponent(threadId) + "/model-executions")
      .then(function (r) { return r.json(); });
  }

  /* List LLM models the user may select for the SDLC coder agent.
   * Returns {models:[name,...], default:name}. Used by the coder view
   * to populate the model dropdown. */
  function getCoderModels() {
    return authFetch("/sdlc/models")
      .then(function (r) { return r.json(); });
  }

  function parseError(text, status) {
    try {
      var j = JSON.parse(text);
      if (j.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      if (j.errors) return (j.errors || []).map(function (e) { return e.message; }).join("; ");
    } catch (e) { /* not json */ }
    return "请求失败 (HTTP " + status + ")";
  }

  global.API = {
    API_BASE: API_BASE,
    getUserToken: getUserToken, setUserToken: setUserToken,
    getSessionToken: getSessionToken, setSessionToken: setSessionToken,
    clearTokens: clearTokens, redirectToLogin: redirectToLogin,
    authFetch: authFetch, userFetch: userFetch,
    register: register, login: login, createSession: createSession,
    listSessions: listSessions, resumeSession: resumeSession,
    getSessionMessages: getSessionMessages,
    deleteSession: deleteSession, renameSession: renameSession,
    streamSSE: streamSSE,
    approve: approve, reject: reject, getSdlcState: getSdlcState,
    getModelExecutions: getModelExecutions,
    getCoderModels: getCoderModels,
  };
})(window);
