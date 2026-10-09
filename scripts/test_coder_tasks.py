"""Run 3 small code-agent tasks through the SDLC coder and verify output.

Each task is a self-contained, verifiable coding micro-task (HumanEval /
LeetCode style). We override coder_model to kimi-k2.7-code (verified quota).
The SDLC pipeline (specing -> planning -> coding -> testing -> reviewing)
runs end-to-end; we capture the generated code_changes and verify them by
executing the target function against the assertions.

Run from host:
    uv run python scripts/test_coder_tasks.py
"""

import json
import os
import sys

import requests

BASE_URL = "http://localhost:8000/api/v1"
# Credentials come from the environment so no secret is committed.
#   export SMOKE_EMAIL="investor_7k3m@example.com" SMOKE_PASSWORD="..."
EMAIL = os.environ.get("SMOKE_EMAIL", "investor_7k3m@example.com")
PASSWORD = os.environ["SMOKE_PASSWORD"]
CODER_MODEL = "kimi-k2.7-code"  # has free quota (verified 200)

# Task tuple layout: name, requirements, target function, verifier source.
# verifier_source is a snippet that uses `fn` (the target function) and must
# raise AssertionError on failure. Executed in a namespace with fn defined.
TASKS = [
    {
        "name": "flatten_nested_list",
        "requirements": (
            "在 app/utils/ 下新增一个函数 flatten_list(nested)，把任意嵌套的列表"
            "(可多层嵌套、含非列表元素、可能含 None)拍平为一维列表。"
            "规则：保持元素原有顺序；只展开 list 类型；忽略 None 元素。"
            "示例：flatten_list([1, [2, [3, 4]], 5]) 应返回 [1, 2, 3, 4, 5]；"
            "flatten_list([None, [1, None], 2]) 应返回 [1, 2]。"
        ),
        "fn": "flatten_list",
        "verify": """
assert fn([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5], fn([1, [2, [3, 4]], 5])
assert fn([]) == []
assert fn([1, 2, 3]) == [1, 2, 3]
assert fn([None, [1, None], 2]) == [1, 2]
assert fn([[["a"], "b"], ["c"]]) == ["a", "b", "c"]
""",
    },
    {
        "name": "deep_merge_dicts",
        "requirements": (
            "在 app/utils/ 下新增一个函数 deep_merge(base, override)，递归深度合并两个嵌套字典。"
            "规则：同键若两边都是 dict 则递归合并；否则 override 的值覆盖 base；"
            "返回一个新的合并字典，不得原地修改 base 或 override。"
            "示例：deep_merge({'a': {'x': 1}}, {'a': {'y': 2}, 'b': 3}) "
            "应返回 {'a': {'x': 1, 'y': 2}, 'b': 3}，且传入的 base 不被修改。"
        ),
        "fn": "deep_merge",
        "verify": """
b = {"a": {"x": 1}}
o = {"a": {"y": 2}, "b": 3}
r = fn(b, o)
assert r == {"a": {"x": 1, "y": 2}, "b": 3}, r
assert b == {"a": {"x": 1}}, "base was mutated"
assert o == {"a": {"y": 2}, "b": 3}, "override was mutated"
assert fn({}, {"k": 1}) == {"k": 1}
assert fn({"k": 1}, {}) == {"k": 1}
""",
    },
    {
        "name": "lru_cache_class",
        "requirements": (
            "在 app/utils/ 下新增一个 LRUCache 类，构造器接收 capacity (int)。"
            "提供方法 get(key) -> 命中返回值，未命中返回 -1；put(key, value) 写入。"
            "容量超过 capacity 时淘汰最久未使用的项。get/put 都算一次访问(更新最近使用)。"
            "O(1) 期望用 dict + 双向链表或 OrderedDict。用示例自测："
            "cap=2; c=LRUCache(2); c.put(1,1); c.put(2,2); c.get(1)==1; "
            "c.put(3,3) 淘汰 key2; c.get(2)==-1; c.put(4,4) 淘汰 key1; c.get(1)==-1; c.get(3)==3; c.get(4)==4。"
        ),
        "fn": "LRUCache",
        "verify": """
c = fn(2)
c.put(1, 1); c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)
assert c.get(2) == -1, "key2 should be evicted"
c.put(4, 4)
assert c.get(1) == -1, "key1 should be evicted"
assert c.get(3) == 3
assert c.get(4) == 4
""",
    },
]


def login() -> str:
    """Log in and return a bearer token."""
    r = requests.post(
        f"{BASE_URL}/auth/login", data={"email": EMAIL, "password": PASSWORD, "grant_type": "password"}, timeout=15
    )
    r.raise_for_status()
    return r.json()["access_token"]


def main() -> int:
    """Run the coder task flow against a live server."""
    token = login()
    print("login_ok")
    user_headers = {"Authorization": f"Bearer {token}"}
    s = requests.post(f"{BASE_URL}/auth/session", headers=user_headers, timeout=15)
    s.raise_for_status()
    session_token = s.json()["token"]["access_token"]
    print("session_ok")
    headers = {"Authorization": f"Bearer {session_token}"}

    results = []
    for i, task in enumerate(TASKS, 1):
        print(f"\n===== TASK {i}/{len(TASKS)}: {task['name']} =====")
        payload = {"requirements": task["requirements"], "coder_model": CODER_MODEL}
        try:
            r = requests.post(f"{BASE_URL}/sdlc/", json=payload, headers=headers, timeout=600)
            print("sdlc_status", r.status_code)
            if r.status_code != 200:
                print("sdlc_err", r.text[:400])
                results.append({"name": task["name"], "ok": False, "error": f"http {r.status_code}"})
                continue
            data = r.json()
            thread_id = data.get("thread_id")
            status = data.get("status")
            code_changes = data.get("code_changes", [])
            print("thread_id", thread_id, "final_status", status, "code_changes", len(code_changes))
            # Find the generated code that defines the target function.
            generated_code = None
            for ch in code_changes:
                content = ch.get("content", "")
                if f"def {task['fn']}" in content or f"class {task['fn']}" in content:
                    generated_code = content
                    break
            if not generated_code and code_changes:
                generated_code = code_changes[0].get("content", "")
            # Verify correctness by executing the generated code + assertions.
            verify_ok, verify_msg = verify(task, generated_code)
            results.append(
                {
                    "name": task["name"],
                    "thread_id": thread_id,
                    "final_status": status,
                    "files": [c.get("file_path") for c in code_changes],
                    "verify_ok": verify_ok,
                    "verify_msg": verify_msg,
                    "code": generated_code,
                }
            )
            print("VERIFY", "PASS" if verify_ok else "FAIL", verify_msg)
        except Exception as e:
            print("task_exception", repr(e))
            results.append({"name": task["name"], "ok": False, "error": repr(e)})

    # Persist summary
    with open("coder_tasks_result.json", "w", encoding="utf8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n===== SUMMARY =====")
    for res in results:
        ok = res.get("verify_ok") or res.get("ok")
        print(
            f"- {res['name']}: {'PASS' if ok else 'FAIL'} (status={res.get('final_status')}, files={res.get('files')})"
        )
    print("WROTE coder_tasks_result.json")
    return 0


def verify(task: dict, code: str):
    """Execute the generated code and check it against the task spec."""
    if not code:
        return False, "no generated code captured"
    ns: dict = {}
    try:
        exec(code, ns)
    except Exception as e:
        return False, f"exec_error: {e!r}"
    fn = ns.get(task["fn"])
    if fn is None:
        return False, f"target {task['fn']} not found in generated code"
    ns["fn"] = fn
    try:
        exec(task["verify"], ns)
    except AssertionError as e:
        return False, f"assertion_failed: {e!r}"
    except Exception as e:
        return False, f"verify_error: {e!r}"
    return True, "all assertions passed"


if __name__ == "__main__":
    sys.exit(main())
