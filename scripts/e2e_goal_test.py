r"""E2E test for Goal persistence feature against running container.

Self-contained: auto-registers a fresh test user + session, so no hardcoded
JWT tokens. Run inside the app container:

    cd /app && /app/.venv/bin/python /app/scripts/e2e_goal_test.py

Or from host (requires app listening on localhost:8000):

    .venv\\Scripts\\python.exe scripts\\e2e_goal_test.py
"""

import uuid

import httpx

BASE = "http://localhost:8000/api/v1"
TIMEOUT = 180

passed = []
failed = []


def check(name, ok, detail=""):
    """Record a pass/fail check result and print it."""
    if ok:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} - {detail}")


def setup_tokens() -> tuple[dict, dict]:
    """Register a fresh user + create a session, return (user_headers, session_headers)."""
    suffix = uuid.uuid4().hex[:8]
    email = f"e2e_goal_{suffix}@test.com"
    username = f"e2e_goal_{suffix}"

    r = httpx.post(
        f"{BASE}/auth/register",
        json={"email": email, "password": "Test1234!", "username": username},  # pragma: allowlist secret
        timeout=30,
    )
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    user_token = r.json()["token"]["access_token"]
    uh = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}

    r = httpx.post(
        f"{BASE}/auth/session",
        headers=uh,
        json={"name": f"e2e-goal-{suffix}"},
        timeout=30,
    )
    assert r.status_code == 200, f"session create failed: {r.status_code} {r.text}"
    session_token = r.json()["token"]["access_token"]
    sh = {"Authorization": f"Bearer {session_token}", "Content-Type": "application/json"}
    return uh, sh


print("=" * 60)
print("E2E Goal Persistence Test")
print("=" * 60)

UH, SH = setup_tokens()
print("  Setup: fresh user + session registered")

# 1. Create a goal
print("\n--- 1. Create Goal ---")
r = httpx.post(
    f"{BASE}/goals/",
    headers=UH,
    json={"title": "E2E Test Goal", "description": "validation"},
    timeout=30,
)
check("create_goal_200", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
goal_id = r.json().get("id") if r.status_code == 200 else None
print(f"  goal_id: {goal_id}")

# 2. List goals
print("\n--- 2. List Goals ---")
r = httpx.get(f"{BASE}/goals/", headers=UH, timeout=30)
check("list_goals_200", r.status_code == 200, f"status={r.status_code}")
check(
    "list_goals_contains_created",
    r.status_code == 200 and any(g.get("id") == goal_id for g in r.json()),
    f"goal_id={goal_id} not in list",
)

if goal_id:
    # 3. Get goal detail (includes tasks + progress)
    print(f"\n--- 3. Get Goal Detail ({goal_id}) ---")
    r = httpx.get(f"{BASE}/goals/{goal_id}", headers=UH, timeout=30)
    check("get_goal_200", r.status_code == 200, f"status={r.status_code}")
    detail = r.json() if r.status_code == 200 else {}
    check("detail_has_tasks", "tasks" in detail, f"keys={list(detail.keys())}")
    check("detail_has_progress", "progress" in detail, f"keys={list(detail.keys())}")
    check(
        "detail_progress_empty", detail.get("progress", {}).get("total", -1) == 0, f"progress={detail.get('progress')}"
    )

    # 4. Update goal (PATCH)
    print("\n--- 4. Update Goal (PATCH) ---")
    r = httpx.patch(
        f"{BASE}/goals/{goal_id}",
        headers=UH,
        json={"title": "E2E Updated Goal"},
        timeout=30,
    )
    check("update_goal_200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        check("update_goal_title", r.json().get("title") == "E2E Updated Goal", f"title={r.json().get('title')}")

    # 5. Create SDLC task with goal_id (tests context injection path)
    #    SDLC endpoint requires session token (get_current_session)
    print("\n--- 5. Create SDLC Task with goal_id ---")
    r = httpx.post(
        f"{BASE}/sdlc/",
        headers=SH,
        json={"requirements": "E2E test: create a simple hello.py file", "goal_id": goal_id},
        timeout=TIMEOUT,
    )
    print(f"  SDLC status: {r.status_code}")
    if r.status_code == 200:
        task_data = r.json()
        thread_id = task_data.get("thread_id") or task_data.get("id")
        check("sdlc_task_created", thread_id is not None, f"keys={list(task_data.keys())}")
        print(f"  thread_id: {thread_id}")
    else:
        check("sdlc_task_created", False, f"status={r.status_code}, body={r.text[:300]}")
        thread_id = None

    # 6. Verify task appears in goal detail with progress
    if thread_id:
        print("\n--- 6. Verify Task-Goal Association ---")
        r = httpx.get(f"{BASE}/goals/{goal_id}", headers=UH, timeout=30)
        if r.status_code == 200:
            detail = r.json()
            tasks = detail.get("tasks", [])
            check("task_in_goal", len(tasks) > 0, f"tasks count={len(tasks)}")
            if tasks:
                check(
                    "task_has_correct_goal_id",
                    tasks[0].get("goal_id") == goal_id,
                    f"task goal_id={tasks[0].get('goal_id')}",
                )
            progress = detail.get("progress", {})
            check("progress_total_1", progress.get("total") == 1, f"progress={progress}")
            print(f"  Progress: {progress}")

    # 7. Verify DB: sdlc_task.goal_id column is populated
    print("\n--- 7. Verify DB: sdlc_task.goal_id populated ---")
    try:
        from app.core.db.session import get_async_session_maker
        import asyncio

        async def check_db():
            """Query DB for tasks with goal_id, return True if any exist."""
            async with get_async_session_maker()() as session:
                from sqlalchemy import text

                result = await session.execute(
                    text("SELECT id, goal_id, status FROM sdlc_task WHERE goal_id = :gid LIMIT 5"),
                    {"gid": goal_id},
                )
                rows = result.fetchall()
                print(f"  Tasks with goal_id={goal_id}: {len(rows)}")
                for row in rows:
                    print(f"    task_id={row[0]}, goal_id={row[1]}, status={row[2]}")
                return len(rows) > 0

        has_tasks = asyncio.run(check_db())
        check("db_task_has_goal_id", has_tasks, "no tasks with goal_id in DB")
    except Exception as e:
        check("db_task_has_goal_id", False, f"exception: {e}")

    # 8. Verify goal isolation (second goal should have no tasks)
    print("\n--- 8. Verify Goal Isolation ---")
    r = httpx.post(
        f"{BASE}/goals/",
        headers=UH,
        json={"title": "Isolation Test Goal", "description": "should have no tasks"},
        timeout=30,
    )
    if r.status_code == 200:
        goal2_id = r.json().get("id")
        r = httpx.get(f"{BASE}/goals/{goal2_id}", headers=UH, timeout=30)
        if r.status_code == 200:
            detail2 = r.json()
            check("goal2_no_tasks", len(detail2.get("tasks", [])) == 0, f"tasks={len(detail2.get('tasks', []))}")
            check(
                "goal2_progress_zero",
                detail2.get("progress", {}).get("total") == 0,
                f"progress={detail2.get('progress')}",
            )

# Summary
print("\n" + "=" * 60)
print(f"RESULTS: {len(passed)} passed, {len(failed)} failed")
print("=" * 60)
if failed:
    print("\nFAILED:")
    for f in failed:
        print(f"  - {f}")
else:
    print("\nAll checks passed!")
