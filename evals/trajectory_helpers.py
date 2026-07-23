"""Helpers for trajectory-level evaluation.

Distinct from ``evals/helpers.py`` (answer-level): trajectory eval needs the
*full ordered message sequence*, not an input/output split, plus budget and
loop analysis. Keeping these separate avoids touching the stable answer-level
helpers (decision C in ``docs/eval-upgrade.md``).
"""

from __future__ import annotations

import json
import os
from typing import Any

# Coarse token estimate (chars / 4). Deliberately dependency-free — used only
# for budget regression assertions, not billing. Real token counts come from
# the LLM usage tracker at runtime.
_CHARS_PER_TOKEN = 4


def format_trajectory(messages: list[dict]) -> str:
    """Format an ordered message sequence as a human-readable trajectory.

    Produces a numbered step list the LLM-as-Judge can score:

    ```
    Step 1 [user]: build a landing page...
    Step 2 [assistant -> tool_call]: write_file({"path": "index.html"})
    Step 3 [tool: write_file]: File written: index.html
    Step 4 [assistant]: Done. Created index.html.
    ```

    Args:
        messages: Ordered message dicts. Each may have ``type``
            (``"human"``/``"assistant"``/``"tool"``), ``content``, and for
            assistant messages ``tool_calls`` or ``additional_kwargs``.

    Returns:
        Newline-joined step strings. Empty string if ``messages`` is empty.
    """
    steps: list[str] = []
    for idx, msg in enumerate(messages, start=1):
        role = msg.get("type") or msg.get("role") or "unknown"
        content = msg.get("content") or ""

        # Assistant with tool_calls — surface the tool + args.
        tool_calls = msg.get("tool_calls") or msg.get("additional_kwargs", {}).get("tool_calls", [])
        if role == "assistant" and tool_calls:
            call_lines = []
            for tc in tool_calls:
                fn = tc.get("function", {}) if "function" in tc else tc
                name = fn.get("name", "?")
                args = fn.get("arguments", fn.get("args", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        pass
                call_lines.append(f"{name}({json.dumps(args, ensure_ascii=False)})")
            steps.append(f"Step {idx} [assistant -> tool_call]: {', '.join(call_lines)}")
        elif role == "tool":
            name = msg.get("name", "?")
            snippet = content[:120] + "..." if len(content) > 120 else content
            steps.append(f"Step {idx} [tool: {name}]: {snippet}")
        else:
            snippet = content[:200] + "..." if len(content) > 200 else content
            steps.append(f"Step {idx} [{role}]: {snippet}")
    return "\n".join(steps)


def format_trajectory_steps(steps: list[dict]) -> str:
    """Format a golden ``expected_trajectory`` step list (dataclass-style).

    Unlike :func:`format_trajectory` (which takes LangChain message dicts),
    this takes the golden-task step dicts (``{step, type, tool, args, ...}``)
    produced in ``evals/golden/tasks.py``.

    Args:
        steps: List of golden trajectory step dicts.

    Returns:
        Newline-joined step strings.
    """
    lines: list[str] = []
    for s in steps:
        step_no = s.get("step", "?")
        stype = s.get("type", "?")
        if stype == "assistant" and "tool" in s:
            args = s.get("args", {})
            lines.append(
                f"Step {step_no} [assistant -> tool_call]: {s['tool']}({json.dumps(args, ensure_ascii=False)})"
            )
        elif stype == "tool":
            lines.append(f"Step {step_no} [tool: {s.get('tool', '?')}]: {s.get('result_snippet', '')}")
        else:
            lines.append(f"Step {step_no} [{stype}]: {s.get('content', s.get('result_snippet', ''))}")
    return "\n".join(lines)


def get_trajectory_messages(trace: Any) -> list[dict]:
    """Extract the full ordered message list from a Langfuse trace.

    Unlike ``helpers.get_input_output`` (which splits into input/output),
    this returns the complete sequence so trajectory rubrics can see every
    tool-call round.

    Args:
        trace: A Langfuse ``TraceWithDetails`` whose ``output`` is a dict
            with a ``messages`` key.

    Returns:
        The message list, or ``[]`` if the trace has no messages.
    """
    output = getattr(trace, "output", None)
    if not isinstance(output, dict):
        return []
    return output.get("messages", []) or []


def estimate_tokens(text: str) -> int:
    """Estimate token count from text length.

    Coarse ``len(text) // 4`` heuristic — dependency-free, used only for
    budget regression assertions. Not for billing.

    Args:
        text: The text to estimate.

    Returns:
        Estimated token count (minimum 1 for non-empty text).
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def detect_redundant_loops(trajectory: list[dict]) -> list[tuple[int, int, str]]:
    """Detect consecutive identical tool-calls in a trajectory.

    A redundant loop is when the agent calls the same tool with the same args
    twice in a row (tool-result steps between them are ignored). This is the
    "no meaningless循环" signal the trajectory rubric scores.

    Args:
        trajectory: List of step dicts (golden ``expected_trajectory`` or a
            derived step list). Assistant tool-call steps must have ``type``
            == ``"assistant"`` and a ``tool`` key.

    Returns:
        List of ``(step_a, step_b, tool)`` tuples for each detected loop,
        where ``step_a``/``step_b`` are the step numbers of the duplicate
        pair. Empty list if no loops.
    """
    # Collect assistant tool-call steps in order.
    tool_steps: list[tuple[int, str, str]] = []
    for s in trajectory:
        if s.get("type") == "assistant" and "tool" in s:
            args = s.get("args", {})
            key = json.dumps(args, sort_keys=True, ensure_ascii=False)
            tool_steps.append((s.get("step", 0), s["tool"], key))

    loops: list[tuple[int, int, str]] = []
    for i in range(1, len(tool_steps)):
        prev_step, prev_tool, prev_args = tool_steps[i - 1]
        cur_step, cur_tool, cur_args = tool_steps[i]
        if prev_tool == cur_tool and prev_args == cur_args:
            loops.append((prev_step, cur_step, cur_tool))
    return loops


_TRAJECTORY_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "metrics", "prompts", "trajectory")


def load_trajectory_metrics() -> list[dict]:
    """Load trajectory rubrics from ``metrics/prompts/trajectory/*.md``.

    Independent of the answer-level loader in ``evals/metrics/__init__.py``
    (which uses non-recursive ``os.listdir`` on the parent ``prompts/`` dir
    and therefore never sees this subdirectory). This keeps trajectory
    rubrics from polluting the answer-level metric set.

    Returns:
        List of ``{"name": str, "prompt": str}`` dicts, one per ``.md``
        file in the trajectory subdirectory. Empty list if the directory
        does not exist.
    """
    trajectory_metrics: list[dict] = []
    if not os.path.isdir(_TRAJECTORY_PROMPTS_DIR):
        return trajectory_metrics
    for file in os.listdir(_TRAJECTORY_PROMPTS_DIR):
        if file.endswith(".md"):
            name = file.replace(".md", "")
            with open(os.path.join(_TRAJECTORY_PROMPTS_DIR, file), "r", encoding="utf-8") as f:
                trajectory_metrics.append({"name": name, "prompt": f.read()})
    return trajectory_metrics
