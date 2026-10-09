"""Debug: replicate the login -> session -> sdlc auth chain and inspect tokens."""

import json
import os
import requests

BASE = "http://localhost:8000/api/v1"
# Credentials come from the environment so no secret is committed.
#   export SMOKE_EMAIL="investor_7k3m@example.com" SMOKE_PASSWORD="..."
EMAIL = os.environ.get("SMOKE_EMAIL", "investor_7k3m@example.com")
PASSWORD = os.environ["SMOKE_PASSWORD"]

# 1) login
r = requests.post(
    f"{BASE}/auth/login", data={"email": EMAIL, "password": PASSWORD, "grant_type": "password"}, timeout=15
)
print("login", r.status_code)
user_tok = r.json()["access_token"]
print("user_tok_head", user_tok[:30])

# 2) session
s = requests.post(f"{BASE}/auth/session", headers={"Authorization": f"Bearer {user_tok}"}, timeout=15)
print("session", s.status_code)
print("session_body", json.dumps(s.json(), ensure_ascii=False)[:500])
sess_tok = s.json().get("token")
print("sess_tok_head", (sess_tok or "")[:30])

# 3) sdlc with session token
r2 = requests.post(
    f"{BASE}/sdlc/", headers={"Authorization": f"Bearer {sess_tok}"}, json={"requirements": "test"}, timeout=30
)
print("sdlc", r2.status_code, r2.text[:300])
