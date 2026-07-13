"""Probe Bailian (DashScope) OpenAI-compatible models for availability.

Uses only the Python standard library (urllib) so it can run inside any
container without third-party HTTP clients installed.

Usage:
    python scripts/test_bailian_models.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import List

# Curated candidate list (2026-07 free-tier catalogue).
CANDIDATES: List[str] = [
    # --- Qwen3.7 series (flagship) ---
    "qwen3.7-max",
    "qwen3.7-max-2026-06-08",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-plus",
    "qwen3.7-plus-2026-05-26",
    # --- Qwen3.6 series ---
    "qwen3.6-max-preview",
    "qwen3.6-plus",
    "qwen3.6-plus-2026-04-02",
    "qwen3.6-flash",
    "qwen3.6-flash-2026-04-16",
    # --- Qwen3.5 series ---
    "qwen3.5-plus",
    "qwen3.5-plus-2026-02-15",
    "qwen3.5-flash",
    "qwen3.5-flash-2026-02-23",
    # --- Qwen3 series ---
    "qwen3-max",
    "qwen3-max-2026-01-23",
    "qwen3-max-preview",
    "qwen3-max-2025-09-23",
    "qwq-plus",
    "qwen3-235b-a22b",
    "qwen3-32b",
    "qwen3-30b-a3b",
    "qwen3-14b",
    "qwen3-8b",
    # --- Qwen3-Coder series ---
    "qwen3-coder-plus",
    "qwen3-coder-plus-2025-09-23",
    "qwen3-coder-plus-2025-07-22",
    "qwen3-coder-flash",
    "qwen3-coder-flash-2025-07-28",
    "qwen3-coder-next",
    # --- Qwen long / legacy ---
    "qwen-long",
    "qwen-long-latest",
    "qwen-plus",
    "qwen-plus-latest",
    "qwen-max",
    "qwen-max-latest",
    "qwen-flash",
    "qwen-turbo",
    # --- DeepSeek series ---
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-v3.2",
    "deepseek-v3.2-exp",
    "deepseek-v3.1",
    "deepseek-v3",
    "deepseek-r1",
    "deepseek-r1-0528",
    # --- Third-party ---
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "glm-4.7",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "MiniMax-M3",
    "mimo-v2.5-pro",
]


def probe(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    """Return (ok, detail)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return False, "no choices in response"
        content = (choices[0].get("message", {}).get("content") or "").strip()
        if not content:
            return False, "empty content"
        return True, content[:40]
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(e)
        return False, f"HTTP {e.code}: {err_body[:140]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:160]


def main() -> int:
    key = os.getenv("OPENAI_API_KEY", "")
    base = os.getenv(
        "OPENAI_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    if not key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    print(f"Key prefix={key[:12]}... len={len(key)}")
    print(f"Base URL: {base}")
    print(f"Probing {len(CANDIDATES)} candidate models...\n")

    ok_models: list[str] = []
    fail_no_quota: list[str] = []
    fail_other: list[tuple[str, str]] = []

    for i, model in enumerate(CANDIDATES, 1):
        ok, detail = probe(key, base, model)
        tag = "OK  " if ok else "FAIL"
        print(f"[{i:>3}/{len(CANDIDATES)}] {tag} {model:<32} | {detail}")
        if ok:
            ok_models.append(model)
        elif (
            "Arrearage" in detail
            or "Access denied" in detail
            or "in good standing" in detail
            or "AllocationQuota" in detail
        ):
            fail_no_quota.append(model)
        else:
            fail_other.append((model, detail))
        time.sleep(0.3)

    print("\n=== SUMMARY ===")
    print(f"OK: {len(ok_models)} / {len(CANDIDATES)}")
    for m in ok_models:
        print(f"  + {m}")
    print(f"\nNo-quota / Access-denied: {len(fail_no_quota)}")
    print(f"Other failures: {len(fail_other)}")
    for m, d in fail_other[:20]:
        print(f"  - {m}: {d[:100]}")

    if ok_models:
        print("\n=== LLM_MODELS line (copy-paste) ===")
        print(",".join(ok_models))
    return 0 if ok_models else 1


if __name__ == "__main__":
    raise SystemExit(main())
