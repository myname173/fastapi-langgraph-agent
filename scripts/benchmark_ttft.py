"""TTFT benchmark for local vLLM prefix caching.

Measures Time-To-First-Token under two scenarios:
1. Cold cache: unique system prompt per request (cache miss)
2. Warm cache: same system prompt (cache hit after first request)

Reports mean / median / min / max / p95 for each scenario.

Run inside the app container:
    docker exec <app> /app/.venv/bin/python /app/scripts/benchmark_ttft.py
"""

import json
import statistics
import time
import uuid

import requests

VLLM_URL = "http://vllm:8001/v1/chat/completions"
MODEL = "local-qwen-1.5b"

# Realistic long system prompt (~600 tokens when repeated 3x).
# Long enough to span many KV blocks (block_size=16) so prefix
# caching benefit is measurable.
BASE_SYSTEM_PROMPT = (
    """你是一个专业的AI投资顾问，拥有以下能力：
1. 实时行情查询：可以查询A股股票的实时价格、涨跌幅、成交量等
2. 基本面分析：可以分析股票的市盈率、市净率、ROE、营收增长率等财务指标
3. 技术面分析：可以计算K线技术指标，如MA、MACD、RSI、布林带等
4. 新闻舆情：可以检索最新新闻并分析市场情绪
5. 多空辩论：可以组织多空双方对标的进行深度研究辩论

请始终以专业、客观、谨慎的态度回答用户问题，并在涉及投资建议时附加风险提示。
你的回答应该结构清晰，数据准确，逻辑严谨。"""
    * 3
)

USER_MSG = "你好，简单介绍一下你自己"

N_ROUNDS = 10


def measure_ttft(system_prompt: str, user_msg: str) -> float:
    """Measure TTFT (seconds) for a single streaming request.

    Sends a streaming chat completion and records the time from
    request dispatch to the first content-bearing token chunk.

    Args:
        system_prompt: The system prompt text.
        user_msg: The user message text.

    Returns:
        TTFT in seconds.

    Raises:
        RuntimeError: If no content token is received within the stream.
    """
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 5,
        "temperature": 0,
        "stream": True,
    }

    start = time.perf_counter()
    resp = requests.post(VLLM_URL, json=payload, stream=True, timeout=30)
    ttft = None
    for line in resp.iter_lines():
        if not line:
            continue
        text = line.decode("utf-8")
        if not text.startswith("data: "):
            continue
        data = text[6:]
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content", "")
            if content:
                ttft = time.perf_counter() - start
                break
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    resp.close()
    if ttft is None:
        raise RuntimeError("No content token received")
    return ttft


def print_stats(label: str, samples: list[float]) -> None:
    """Print statistics for a list of TTFT samples."""
    samples_sorted = sorted(samples)
    p95_idx = min(int(len(samples_sorted) * 0.95), len(samples_sorted) - 1)
    p95 = samples_sorted[p95_idx]
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Rounds:    {len(samples)}")
    print(f"  Mean:      {statistics.mean(samples) * 1000:.1f} ms")
    print(f"  Median:    {statistics.median(samples) * 1000:.1f} ms")
    print(f"  Min:       {min(samples) * 1000:.1f} ms")
    print(f"  Max:       {max(samples) * 1000:.1f} ms")
    print(f"  P95:       {p95 * 1000:.1f} ms")
    print(f"  StdDev:    {statistics.stdev(samples) * 1000:.1f} ms")


def main() -> None:
    """Run the TTFT benchmark and print results."""
    print(f"Model:          {MODEL}")
    print(f"Rounds/scene:   {N_ROUNDS}")
    print(f"Prompt length:  ~{len(BASE_SYSTEM_PROMPT)} chars")
    print(f"vLLM endpoint:  {VLLM_URL}")

    # --- Scenario A: Cold cache (unique prefix, cache miss every time) ---
    print("\n[1/2] Cold cache (unique system prompt per request)...")
    cold_samples = []
    for i in range(N_ROUNDS):
        # Inject a UUID at the start so the prefix is different each time,
        # forcing vLLM to recompute the full prefill (cache miss).
        unique_prompt = f"[{uuid.uuid4()}]\n{BASE_SYSTEM_PROMPT}"
        ttft = measure_ttft(unique_prompt, USER_MSG)
        cold_samples.append(ttft)
        print(f"  Round {i + 1:2d}: {ttft * 1000:.1f} ms")
        time.sleep(0.5)

    print_stats("Cold Cache (cache miss)", cold_samples)

    # --- Scenario B: Warm cache (same prefix, cache hit) ---
    print("\n[2/2] Warm cache (same system prompt, cache hit)...")
    warm_samples = []
    # First request to populate the cache (not counted in stats).
    print("  (warming cache...)")
    _ = measure_ttft(BASE_SYSTEM_PROMPT, USER_MSG)
    time.sleep(1)

    for i in range(N_ROUNDS):
        ttft = measure_ttft(BASE_SYSTEM_PROMPT, USER_MSG)
        warm_samples.append(ttft)
        print(f"  Round {i + 1:2d}: {ttft * 1000:.1f} ms")
        time.sleep(0.5)

    print_stats("Warm Cache (cache hit)", warm_samples)

    # --- Summary ---
    cold_mean = statistics.mean(cold_samples)
    warm_mean = statistics.mean(warm_samples)
    if warm_mean > 0:
        speedup = cold_mean / warm_mean
        improvement = (1 - warm_mean / cold_mean) * 100
    else:
        speedup = float("inf")
        improvement = 100.0

    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Cold mean TTFT:  {cold_mean * 1000:.1f} ms")
    print(f"  Warm mean TTFT:  {warm_mean * 1000:.1f} ms")
    print(f"  Speedup:         {speedup:.1f}x")
    print(f"  Improvement:     {improvement:.1f}%")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
