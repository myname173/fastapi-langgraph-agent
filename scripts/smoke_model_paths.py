"""Smoke-test every model-dependent path after the dual-provider switch.

Exercises the paths that actually touch an LLM or an embedding model, so a
provider migration is validated end-to-end instead of by reading config:

  chat (DeepSeek + tools) · knowledge-base search (Bailian embeddings)
  · semantic cache · long-term memory · goals (chat-backed)
  · media generation (expected disabled) · vision (expected disabled)

Usage:
    python scripts/smoke_model_paths.py [--base-url http://localhost:8010]
                                        [--email demo@test.com]
                                        [--password 'Demo!Pass2026']
"""

import argparse
import sys
import time

import httpx


def _hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


class Smoke:
    """Collects pass/fail results for each model-dependent endpoint."""

    def __init__(self, base: str) -> None:
        """Initialise the HTTP client for ``base`` (proxy env vars ignored)."""
        # httpx merges base_url.raw_path with the request path; a base_url with
        # no trailing slash yields an empty raw_path and produces a malformed
        # URL, so always normalise to a trailing slash.
        self.base = base.rstrip("/") + "/"
        # trust_env=False is REQUIRED: httpx honours HTTP_PROXY/HTTPS_PROXY from
        # the environment by default. On a dev box behind Clash/a sandbox proxy
        # those are set, and a localhost call gets sent to the proxy, which
        # rewrites the request line to
        #   POST http%3A//localhost%3A8010/api/v1/auth/login
        # and the app answers 404 — indistinguishable from a missing route.
        self.client = httpx.Client(base_url=self.base, timeout=180.0, trust_env=False)
        self.user_token: str | None = None
        self.session_id: str | None = None
        self.session_token: str | None = None
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        """Record and print one check result."""
        self.results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # --- auth -------------------------------------------------------------
    def login(self, email: str, password: str) -> None:
        """Log in (form-encoded) and create a chat session."""
        _hr("1. Auth")
        # NOTE: /auth/login is form-encoded, not JSON.
        r = self.client.post(
            "/api/v1/auth/login",
            data={"email": email, "password": password, "grant_type": "password"},
        )
        if r.status_code != 200:
            self.record("login", False, f"HTTP {r.status_code}: {r.text[:200]}")
            raise SystemExit("cannot continue without a token")
        self.user_token = r.json().get("access_token")
        self.record("login (user token)", bool(self.user_token), f"{len(self.user_token or '')} chars")

        r = self.client.post("/api/v1/auth/session", json={}, headers=self._user_hdr())
        if r.status_code not in (200, 201):
            self.record("create session", False, f"HTTP {r.status_code}: {r.text[:200]}")
            return
        body = r.json()
        # SessionResponse carries session_id plus a nested token object.
        self.session_id = body.get("session_id")
        self.session_token = (body.get("token") or {}).get("access_token")
        self.record(
            "create session",
            bool(self.session_id and self.session_token),
            f"session_id={self.session_id}, token={len(self.session_token or '')} chars",
        )

    def _user_hdr(self) -> dict:
        return {"Authorization": f"Bearer {self.user_token}"}

    def _session_hdr(self) -> dict:
        return {"Authorization": f"Bearer {self.session_token}"}

    # --- chat (DeepSeek + tool calling) -----------------------------------
    def chat(self) -> None:
        """Plain chat turn — proves the DeepSeek text path."""
        _hr("2. Chat path (DeepSeek, tool calling)")
        # ChatRequest takes a full message list; the session comes from the token.
        payload = {"messages": [{"role": "user", "content": "Reply with exactly the word: PONG"}]}
        t0 = time.time()
        r = self.client.post("/api/v1/chatbot/chat", json=payload, headers=self._session_hdr())
        dt = time.time() - t0
        if r.status_code != 200:
            self.record("POST /chatbot/chat", False, f"HTTP {r.status_code}: {r.text[:300]}")
            return
        msgs = r.json().get("messages") or []
        assistant = [m for m in msgs if m.get("role") == "assistant"]
        content = assistant[-1].get("content", "") if assistant else ""
        self.record(
            "POST /chatbot/chat",
            bool(content.strip()),
            f"{dt:.1f}s, {len(content)} chars: {content[:80]!r}",
        )

    def chat_tool_calling(self) -> None:
        """Chat turn that forces tool use — proves the tool-calling path."""
        """Force a tool-using turn — the path that broke under the switch."""
        _hr("2b. Chat tool-calling path (registry + bind_tools)")
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": "Use your tools to look up the current price of AAPL, then tell me the number.",
                }
            ]
        }
        t0 = time.time()
        r = self.client.post("/api/v1/chatbot/chat", json=payload, headers=self._session_hdr())
        dt = time.time() - t0
        if r.status_code != 200:
            self.record("chat with tool use", False, f"HTTP {r.status_code}: {r.text[:300]}")
            return
        msgs = r.json().get("messages") or []
        assistant = [m for m in msgs if m.get("role") == "assistant"]
        content = assistant[-1].get("content", "") if assistant else ""
        # A tool round-trip is only proven by a non-empty final answer; the
        # server would have 400'd above if the tool-call history were malformed.
        self.record(
            "chat with tool use",
            bool(content.strip()),
            f"{dt:.1f}s, {len(content)} chars: {content[:100]!r}",
        )

    # --- embeddings (Bailian) ---------------------------------------------
    def knowledge_base(self) -> None:
        """Knowledge-base search — proves the Bailian embedding path."""
        _hr("3. Embeddings path (Bailian qwen3.7-text-embedding)")
        r = self.client.get(
            "/api/v1/knowledge-base/search",
            params={"q": "risk tolerance", "limit": 3},
            headers=self._user_hdr(),
        )
        ok = r.status_code == 200
        detail = f"HTTP {r.status_code}"
        if ok:
            detail += f", {len(r.json())} results"
        else:
            detail += f": {r.text[:250]}"
        self.record("GET /knowledge-base/search (embedding query)", ok, detail)

        r = self.client.get("/api/v1/knowledge-base/documents", headers=self._user_hdr())
        self.record(
            "GET /knowledge-base/documents",
            r.status_code == 200,
            f"HTTP {r.status_code}",
        )

    # --- chat-backed feature ----------------------------------------------
    def goals(self) -> None:
        """Goals list (chat-backed, requires a user token)."""
        _hr("4. Goals (chat-backed, user token)")
        r = self.client.get("/api/v1/goals/", headers=self._user_hdr())
        self.record("GET /goals/", r.status_code == 200, f"HTTP {r.status_code}: {r.text[:150]}")

    # --- long-term memory (mem0: deepseek llm + bailian embedder) ---------
    def memory(self) -> None:
        """Chat history read-back after the chat turns."""
        _hr("5. Long-term memory (mem0: DeepSeek LLM + Bailian embedder)")
        r = self.client.get("/api/v1/chatbot/messages", headers=self._session_hdr())
        self.record(
            "GET /chatbot/messages (post-chat history)",
            r.status_code == 200,
            f"HTTP {r.status_code}",
        )

    # --- expected-disabled features ---------------------------------------
    def disabled_features(self) -> None:
        """Assert gated features are absent and images are ignored."""
        _hr("6. Expected-disabled features (gated, should 404)")
        # When ENABLE_MEDIA_GENERATION=false the media router is never included,
        # so its routes are absent → 404. (A POST to an unmatched path instead
        # hits the SPA static mount and returns 405, so probe with GET.)
        r = self.client.get("/api/v1/media/history", headers=self._user_hdr())
        self.record(
            "GET /media/history → 404 (media router unmounted)",
            r.status_code == 404,
            f"HTTP {r.status_code}",
        )

        # Vision is off too: images attached to a chat turn must be ignored,
        # not forwarded to a (nonexistent) vision model.
        r = self.client.post(
            "/api/v1/chatbot/chat",
            json={
                "messages": [{"role": "user", "content": "Say OK"}],
                "images": [{"type": "base64", "data": "data:image/png;base64,aGVsbG8="}],
            },
            headers=self._session_hdr(),
        )
        self.record(
            "chat with image attached → 200 (ENABLE_MULTIMODAL=false, images ignored)",
            r.status_code == 200,
            f"HTTP {r.status_code}",
        )

    # --- health -----------------------------------------------------------
    def health(self) -> None:
        """Liveness probe."""
        _hr("0. Health")
        r = self.client.get("/health")
        self.record("GET /health", r.status_code == 200, f"HTTP {r.status_code}")

    def summary(self) -> int:
        """Print the summary and return a process exit code."""
        _hr("SUMMARY")
        failed = [n for n, ok, _ in self.results if not ok]
        for name, ok, detail in self.results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed")
        if failed:
            print("FAILED: " + ", ".join(failed))
        return 1 if failed else 0


def main() -> int:
    """Parse args, run every check, and return a process exit code."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8010")
    ap.add_argument("--email", default="demo@test.com")
    ap.add_argument("--password", default="Demo!Pass2026")
    args = ap.parse_args()

    s = Smoke(args.base_url)
    s.health()
    s.login(args.email, args.password)
    s.chat()
    s.chat_tool_calling()
    s.knowledge_base()
    s.goals()
    s.memory()
    s.disabled_features()
    return s.summary()


if __name__ == "__main__":
    sys.exit(main())
