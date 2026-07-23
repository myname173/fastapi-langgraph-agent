"""Golden task definitions — the single source of truth for trajectory evals.

Each :class:`GoldenTask` captures the *expected* behaviour of the coder agent
for a representative requirement: which tools it should call, in what order,
within what step/token budget. The regression gate replays a mock LLM that
emits the expected tool-calls and asserts ``coder_node`` reproduces the
trajectory; the offline LLM-as-Judge scores *real* traces against the same
rubrics.

Adding a new golden task:

1. Append a :class:`GoldenTask` to ``GOLDEN_TASKS`` below.
2. Ensure ``expected_tools`` are registered tool names (see
   ``app/core/langgraph/tools/sdlc_tools.py``) or ``mcp_*`` prefixed.
3. Ensure ``len(expected_trajectory) <= max_steps``.
4. Ensure ``estimate_tokens(requirement) + sum(estimate_tokens(str(s)) for s
   in expected_trajectory) <= max_tokens``.

The regression tests in ``tests/test_eval_regression.py`` enforce 2-4
automatically, so a malformed golden fails CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GoldenTask:
    """One expected agent trajectory for a representative SDLC task.

    Attributes:
        task_type: Short identifier (also the sqlite ``task_type`` key).
        requirement: The user-facing requirement string fed to the coder.
        expected_files: Files the agent is expected to touch/create.
        expected_tools: Registered tool names the trajectory should use.
        expected_trajectory: Ordered step list. Each step is a dict with
            ``step`` (int), ``type`` (``"assistant"`` | ``"tool"``), and
            either ``tool``+``args``+``result_snippet`` (for tool-call
            rounds) or ``content`` (for the final text answer).
        max_steps: Upper bound on trajectory length (regression assertion).
        max_tokens: Upper bound on estimated token usage (regression
            assertion). Coarse: ``len(text) // 4``.
    """

    task_type: str
    requirement: str
    expected_files: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    expected_trajectory: list[dict] = field(default_factory=list)
    max_steps: int = 8
    max_tokens: int = 5000


def _assistant_tool(step: int, tool: str, args: dict, snippet: str) -> dict:
    """Build an assistant tool-call step."""
    return {
        "step": step,
        "type": "assistant",
        "tool": tool,
        "args": args,
        "result_snippet": snippet,
    }


def _tool_result(step: int, tool: str, snippet: str) -> dict:
    """Build a tool-result step."""
    return {
        "step": step,
        "type": "tool",
        "tool": tool,
        "result_snippet": snippet,
    }


def _final(step: int, content: str) -> dict:
    """Build the final assistant text step."""
    return {"step": step, "type": "assistant", "content": content}


# ---------------------------------------------------------------------------
# 8 golden tasks — diverse scenarios, tool sets, and budgets.
# Task 8 (mcp_lookup) ties back to the MCP integration for narrative continuity.
# ---------------------------------------------------------------------------

GOLDEN_TASKS: list[GoldenTask] = [
    GoldenTask(
        task_type="landing_page",
        requirement="Build a landing page with a hero section and a contact form.",
        expected_files=["index.html", "styles.css"],
        expected_tools=["write_file", "edit_file"],
        expected_trajectory=[
            _assistant_tool(
                1, "write_file", {"path": "index.html", "content": "<!DOCTYPE html>..."}, "create index.html"
            ),
            _tool_result(2, "write_file", "File written: index.html"),
            _assistant_tool(
                3,
                "edit_file",
                {"path": "styles.css", "old_str": "", "new_str": "body { margin: 0; }"},
                "add hero styles",
            ),
            _tool_result(4, "edit_file", "Edited: styles.css"),
            _final(5, "Done. Created index.html and styles.css."),
        ],
        max_steps=6,
        max_tokens=4000,
    ),
    GoldenTask(
        task_type="script",
        requirement="Create a Python script to fetch stock prices from an API.",
        expected_files=["fetch.py"],
        expected_tools=["write_file", "run_tests"],
        expected_trajectory=[
            _assistant_tool(1, "write_file", {"path": "fetch.py", "content": "import requests..."}, "create fetch.py"),
            _tool_result(2, "write_file", "File written: fetch.py"),
            _assistant_tool(3, "run_tests", {"path": "fetch.py"}, "run unit tests"),
            _tool_result(4, "run_tests", "All tests passed"),
            _final(5, "Done. Created fetch.py and verified with tests."),
        ],
        max_steps=6,
        max_tokens=3000,
    ),
    GoldenTask(
        task_type="bug_fix",
        requirement="Fix the null pointer bug in the auth module when user_id is missing.",
        expected_files=["auth.py"],
        expected_tools=["read_file", "edit_file", "run_tests"],
        expected_trajectory=[
            _assistant_tool(1, "read_file", {"path": "auth.py"}, "inspect auth module"),
            _tool_result(2, "read_file", "Returned: auth.py contents"),
            _assistant_tool(
                3,
                "edit_file",
                {"path": "auth.py", "old_str": "return user.id", "new_str": "return user.id if user else None"},
                "guard against None user",
            ),
            _tool_result(4, "edit_file", "Edited: auth.py"),
            _assistant_tool(5, "run_tests", {"path": "auth.py"}, "verify fix"),
            _tool_result(6, "run_tests", "All tests passed"),
            _final(7, "Fixed the null pointer by guarding against missing user."),
        ],
        max_steps=8,
        max_tokens=5000,
    ),
    GoldenTask(
        task_type="refactor",
        requirement="Extract the repeated validation logic in utils.py into a single helper function.",
        expected_files=["utils.py"],
        expected_tools=["read_file", "edit_file", "run_tests"],
        expected_trajectory=[
            _assistant_tool(1, "read_file", {"path": "utils.py"}, "inspect utils module"),
            _tool_result(2, "read_file", "Returned: utils.py contents"),
            _assistant_tool(
                3,
                "edit_file",
                {"path": "utils.py", "old_str": "def check_a", "new_str": "def _validate...\ndef check_a"},
                "extract helper",
            ),
            _tool_result(4, "edit_file", "Edited: utils.py"),
            _assistant_tool(5, "run_tests", {"path": "utils.py"}, "verify refactor"),
            _tool_result(6, "run_tests", "All tests passed"),
            _final(7, "Refactored validation into a shared _validate helper."),
        ],
        max_steps=8,
        max_tokens=5000,
    ),
    GoldenTask(
        task_type="test_addition",
        requirement="Add unit tests for the pricing module's discount calculation.",
        expected_files=["test_pricing.py"],
        expected_tools=["read_file", "write_file", "run_tests"],
        expected_trajectory=[
            _assistant_tool(1, "read_file", {"path": "pricing.py"}, "inspect pricing module"),
            _tool_result(2, "read_file", "Returned: pricing.py contents"),
            _assistant_tool(
                3, "write_file", {"path": "test_pricing.py", "content": "def test_discount..."}, "create test file"
            ),
            _tool_result(4, "write_file", "File written: test_pricing.py"),
            _assistant_tool(5, "run_tests", {"path": "test_pricing.py"}, "run new tests"),
            _tool_result(6, "run_tests", "All tests passed"),
            _final(7, "Added unit tests covering discount edge cases."),
        ],
        max_steps=8,
        max_tokens=4000,
    ),
    GoldenTask(
        task_type="config_update",
        requirement="Update docker-compose.yml to add a redis service on port 6379.",
        expected_files=["docker-compose.yml"],
        expected_tools=["read_file", "edit_file"],
        expected_trajectory=[
            _assistant_tool(1, "read_file", {"path": "docker-compose.yml"}, "inspect compose file"),
            _tool_result(2, "read_file", "Returned: docker-compose.yml contents"),
            _assistant_tool(
                3,
                "edit_file",
                {"path": "docker-compose.yml", "old_str": "services:", "new_str": "services:\n  redis:"},
                "add redis service",
            ),
            _tool_result(4, "edit_file", "Edited: docker-compose.yml"),
            _final(5, "Added redis service to docker-compose.yml."),
        ],
        max_steps=6,
        max_tokens=2500,
    ),
    GoldenTask(
        task_type="api_endpoint",
        requirement="Add a /health endpoint to the FastAPI app that returns status ok.",
        expected_files=["main.py"],
        expected_tools=["read_file", "edit_file", "run_tests"],
        expected_trajectory=[
            _assistant_tool(1, "read_file", {"path": "main.py"}, "inspect main module"),
            _tool_result(2, "read_file", "Returned: main.py contents"),
            _assistant_tool(
                3,
                "edit_file",
                {"path": "main.py", "old_str": "app = FastAPI()", "new_str": "app = FastAPI()\n@app.get('/health')"},
                "add health route",
            ),
            _tool_result(4, "edit_file", "Edited: main.py"),
            _assistant_tool(5, "run_tests", {"path": "main.py"}, "verify endpoint"),
            _tool_result(6, "run_tests", "All tests passed"),
            _final(7, "Added /health endpoint returning status ok."),
        ],
        max_steps=8,
        max_tokens=5000,
    ),
    GoldenTask(
        task_type="mcp_lookup",
        requirement="Find similar past tasks via MCP, then fix a typo in the task file.",
        expected_files=["task.py"],
        expected_tools=["mcp_query_tasks", "read_file", "edit_file"],
        expected_trajectory=[
            _assistant_tool(1, "mcp_query_tasks", {"status": "completed", "limit": 5}, "find similar past work"),
            _tool_result(2, "mcp_query_tasks", "Returned: 3 completed tasks"),
            _assistant_tool(3, "read_file", {"path": "task.py"}, "inspect task file"),
            _tool_result(4, "read_file", "Returned: task.py contents"),
            _assistant_tool(
                5, "edit_file", {"path": "task.py", "old_str": "recieve", "new_str": "receive"}, "fix typo"
            ),
            _tool_result(6, "edit_file", "Edited: task.py"),
            _final(7, "Found similar tasks via MCP and fixed the typo in task.py."),
        ],
        max_steps=8,
        max_tokens=4000,
    ),
]
