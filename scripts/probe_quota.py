"""Probe which models still have free-tier quota.

Hits the chat endpoint with an explicit model override where supported, and a
raw LLM call otherwise. We use the chatbot endpoint (default model =
qwen3.8-max) and also try a direct call via the llm service if exposed.
Simplest: just test the chatbot (qwen3.8-max).
"""

import requests
import os

BASE = "http://localhost:8000/api/v1"
# Credentials come from the environment so no secret is committed.
#   export SMOKE_EMAIL="investor_7k3m@example.com" SMOKE_PASSWORD="..."
EMAIL = os.environ.get("SMOKE_EMAIL", "investor_7k3m@example.com")
PASSWORD = os.environ["SMOKE_PASSWORD"]

t = requests.post(
    f"{BASE}/auth/login", data={"email": EMAIL, "password": PASSWORD, "grant_type": "password"}, timeout=15
).json()["access_token"]
s = requests.post(f"{BASE}/auth/session", headers={"Authorization": f"Bearer {t}"}, timeout=15).json()["token"][
    "access_token"
]
print("auth_ok")

resp = requests.post(
    f"{BASE}/chatbot/",
    headers={"Authorization": f"Bearer {s}"},
    json={"message": "reply with the single word: pong"},
    timeout=90,
)
print("chat_status", resp.status_code)
body = resp.text[:500]
print(body)
