"""E2E test for PO agent — verifies product_spec is produced and consumed."""

import uuid

import httpx

BASE = "http://localhost:8000/api/v1"
TIMEOUT = 300

# 1. Register + session
suffix = uuid.uuid4().hex[:8]
r = httpx.post(
    f"{BASE}/auth/register",
    json={"email": f"po_e2e_{suffix}@test.com", "password": "Test1234!", "username": f"po_e2e_{suffix}"},
    timeout=30,
)
assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
uh = {
    "Authorization": f"Bearer {r.json()['token']['access_token']}",
    "Content-Type": "application/json",
}

r = httpx.post(f"{BASE}/auth/session", headers=uh, json={"name": f"po-e2e-{suffix}"}, timeout=30)
assert r.status_code == 200, f"session failed: {r.status_code} {r.text}"
sh = {
    "Authorization": f"Bearer {r.json()['token']['access_token']}",
    "Content-Type": "application/json",
}
print("SETUP_OK")

# 2. Create SDLC task
r = httpx.post(
    f"{BASE}/sdlc/",
    headers=sh,
    json={"requirements": "Create a simple Python script that prints hello world"},
    timeout=TIMEOUT,
)
print(f"SDLC_STATUS: {r.status_code}")
if r.status_code != 200:
    print(f"SDLC_BODY: {r.text[:500]}")
    raise SystemExit(1)

task_data = r.json()
thread_id = task_data.get("thread_id") or task_data.get("id")
print(f"THREAD_ID: {thread_id}")
print(f"TASK_STATUS: {task_data.get('status')}")

# 3. Check product_spec in state
r = httpx.get(f"{BASE}/sdlc/{thread_id}/state", headers=sh, timeout=30)
print(f"STATE_STATUS: {r.status_code}")
if r.status_code == 200:
    state = r.json()
    product_spec = state.get("product_spec", [])
    print(f"PRODUCT_SPEC_LEN: {len(product_spec)}")
    if product_spec:
        spec = product_spec[0]
        print(f"SPEC_FALLBACK: {spec.get('fallback', False)}")
        print(f"SPEC_SUMMARY: {str(spec.get('summary', ''))[:100]}")
        print(f"SPEC_PROBLEM: {str(spec.get('problem_statement', ''))[:100]}")
        print(f"SPEC_METRICS_COUNT: {len(spec.get('success_metrics', []))}")
        print(f"SPEC_AC_COUNT: {len(spec.get('acceptance_criteria', []))}")
        print(f"SPEC_MVP: {str(spec.get('mvp_recommendation', ''))[:100]}")
        print(f"SPEC_NEEDS_CLARIFICATION: {spec.get('needs_clarification', False)}")
    else:
        print("PRODUCT_SPEC_EMPTY")

    # 4. Check plan exists (planner consumed spec)
    plan = state.get("plan", [])
    print(f"PLAN_LEN: {len(plan)}")
    if plan:
        print(f"PLAN_FIRST_TASK: {str(plan[0].get('description', ''))[:80]}")
else:
    print(f"STATE_BODY: {r.text[:300]}")

print("E2E_PO_DONE")
