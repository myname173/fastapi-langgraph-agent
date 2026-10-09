"""Probe DeepSeek's thinking-mode parameter names for tool calling.

Background: DeepSeek's thinking mode requires ``reasoning_content`` to be echoed
back on every multi-turn tool call. LangChain drops it, so a multi-turn tool
conversation 400s with::

    The 'reasoning_content' in the thinking mode must be passed back to the API

The fix is to disable thinking, but the parameter name is provider-specific and
NOT interchangeable:

  * DashScope / Bailian uses ``enable_thinking``
  * DeepSeek uses ``thinking={"type": "disabled"}`` (or ``reasoning_effort="none"``)
    and **silently ignores** ``enable_thinking``

This probe verifies which spellings the live endpoint *accepts*, so a provider
switch can be validated rather than assumed.

Caveat (measured 2026-10-06): a short 2-turn tool call succeeds under EVERY
variant, including the no-parameter baseline — the 400 only shows up on longer
tool conversations where the model actually enters thinking mode. So a green run
here does NOT prove the thinking-disable is unnecessary; it only proves the
parameter is accepted (not rejected with a 400/422). The end-to-end proof is
``scripts/smoke_model_paths.py`` (chat + tool use) and ``scripts/smoke_sdlc.py``.

Usage:
    python evals/_probe_deepseek.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_SECRETS = os.path.join(os.path.dirname(__file__), "..", "secrets", "chat_api_key.txt")
with open(_SECRETS, "r", encoding="utf-8") as fh:
    _key = fh.read().strip()

from openai import OpenAI  # noqa: E402  (must import after env setup)
from openai.types.chat import (  # noqa: E402
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

BASE_URL = os.getenv("CHAT_BASE_URL", "https://api.deepseek.com")
MODELS = ["deepseek-flash", "deepseek-v4-pro"]

# A tool definition forces the model down the tool-calling path, which is where
# the reasoning_content requirement bites. A plain chat completion will NOT
# reproduce the bug, so the probe must offer a tool.
_TOOLS: list[ChatCompletionToolParam] = [
    {
        "type": "function",
        "function": {
            "name": "get_price",
            "description": "Get the current price of a ticker",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    }
]

# Parameter spellings to test, as raw request-body fields. They must go through
# ``extra_body``: the OpenAI SDK rejects unknown top-level kwargs, so passing
# ``thinking=...`` directly raises TypeError and proves nothing about the API.
_VARIANTS: list[tuple[str, dict]] = [
    ("(none)", {}),
    ("enable_thinking=False (DashScope name — expected to be ignored)", {"enable_thinking": False}),
    ('thinking={"type":"disabled"} (DeepSeek native)', {"thinking": {"type": "disabled"}}),
    ('reasoning_effort="none"', {"reasoning_effort": "none"}),
    ('reasoning_effort="low"', {"reasoning_effort": "low"}),
]


def _probe(client: OpenAI, model: str, label: str, extra: dict) -> None:
    """Run one tool-calling turn and report whether it survived."""
    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": "What is the price of AAPL? Use the tool."},
    ]
    t0 = time.time()
    try:
        first = client.chat.completions.create(model=model, messages=messages, tools=_TOOLS, extra_body=extra)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {label:58} {type(e).__name__}: {str(e)[:110]}", flush=True)
        return

    msg = first.choices[0].message
    if not msg.tool_calls:
        print(f"  WARN  {label:58} no tool_call returned ({time.time() - t0:.1f}s)", flush=True)
        return

    # Second turn: this is the call that 400s when reasoning_content is missing.
    messages.append(
        {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
                if tc.type == "function"
            ],
        }
    )
    messages.append({"role": "tool", "tool_call_id": msg.tool_calls[0].id, "content": "332.89"})
    try:
        second = client.chat.completions.create(model=model, messages=messages, tools=_TOOLS, extra_body=extra)
        content = second.choices[0].message.content or ""
        print(f"  OK    {label:58} multi-turn tool call OK ({time.time() - t0:.1f}s): {content[:50]!r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {label:58} {type(e).__name__}: {str(e)[:110]}", flush=True)


def main() -> int:
    client = OpenAI(api_key=_key, base_url=BASE_URL, timeout=60, max_retries=0)
    print(f"base_url={BASE_URL}")
    for model in MODELS:
        print(f"\n=== {model} ===", flush=True)
        for label, extra in _VARIANTS:
            _probe(client, model, label, extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
