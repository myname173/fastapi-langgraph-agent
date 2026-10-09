"""Unit test for VLLMHealthMonitor event emission.

Validates the logic without needing a live vLLM instance to actually preempt.

Strategy: feed the monitor's internal ``_evaluate`` method a sequence
of mock ``Dict[str, float]`` payloads and assert it logs the right
events at the right thresholds.
"""

import logging
import sys
from pathlib import Path

# Anchor to the project root so ``app.*`` resolves. The test file
# lives in the project root itself. Detect the project root by
# walking up from this file.
import os

PROJECT_ROOT = Path(__file__).resolve().parent
candidate = PROJECT_ROOT
for _ in range(6):
    if (candidate / "app" / "core" / "vllm_health.py").exists():
        PROJECT_ROOT = candidate
        break
    candidate = candidate.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # so .env.development is picked up by app.core.config

from app.core.vllm_health import VLLMHealthMonitor  # noqa: E402


class _CapturingHandler(logging.Handler):
    """Capture every log record's formatted structlog payload as JSON."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # structlog's JSONRenderer puts the final dict in record.msg
            payload = record.msg if isinstance(record.msg, dict) else {"message": str(record.msg)}
            payload["_level"] = record.levelname
            self.records.append(payload)
        except Exception:
            self.handleError(record)


def _make_monitor() -> tuple[VLLMHealthMonitor, _CapturingHandler]:
    """Build a monitor with thresholds tuned tight so the test is fast."""
    # Patch settings-like attributes directly on the instance
    mon = VLLMHealthMonitor()
    # Lower intervals don't apply to _evaluate; thresholds do.
    mon._preempt_warn = 1
    mon._kv_warn = 0.80
    mon._kv_critical = 0.95
    mon._interval = 30
    # Capture log output
    handler = _CapturingHandler()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Drop any existing handlers from prior imports
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    return mon, handler


def test_baseline_event_emitted() -> None:
    """The first sample establishes a baseline and logs it."""
    mon, cap = _make_monitor()
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 10.0, "vllm:request_success_total": 5.0}
    )
    events = [r.get("event") for r in cap.records]
    assert "vllm_health_baseline" in events, f"missing baseline: {events}"
    print("OK: baseline emitted on first sample")


def test_no_event_on_zero_delta_and_low_kv() -> None:
    """No event fires when nothing changed and the KV cache is low."""
    mon, cap = _make_monitor()
    # First sample: baseline
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 10.0, "vllm:request_success_total": 5.0}
    )
    cap.records.clear()
    # Second sample: no preemption, no KV pressure
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 12.0, "vllm:request_success_total": 7.0}
    )
    events = [r.get("event") for r in cap.records]
    assert "vllm_preemption_detected" not in events
    assert "vllm_kv_cache_high" not in events
    assert "vllm_kv_cache_critical" not in events
    print("OK: clean state emits no warnings")


def test_preemption_warning_and_escalation() -> None:
    """Rising preemption counts log a warning, then escalate to critical."""
    mon, cap = _make_monitor()
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 10.0, "vllm:request_success_total": 5.0}
    )
    cap.records.clear()
    # First preempt: WARNING
    mon._evaluate(
        {"vllm:num_preemptions_total": 1.0, "vllm:gpu_cache_usage_perc": 30.0, "vllm:request_success_total": 8.0}
    )
    warn = [r for r in cap.records if r.get("event") == "vllm_preemption_detected"]
    assert len(warn) == 1, f"expected 1 preemption event, got {len(warn)}"
    assert warn[0]["_level"] == "WARNING", f"expected WARNING, got {warn[0]['_level']}"
    assert warn[0]["delta"] == 1
    assert warn[0]["total"] == 1
    assert warn[0]["severity"] == "warning"
    print(f"OK: first preemption = WARNING delta={warn[0]['delta']} total={warn[0]['total']}")

    # 2nd preempt: still WARNING (streak=2)
    cap.records.clear()
    mon._evaluate(
        {"vllm:num_preemptions_total": 2.0, "vllm:gpu_cache_usage_perc": 35.0, "vllm:request_success_total": 10.0}
    )
    warn2 = [r for r in cap.records if r.get("event") == "vllm_preemption_detected"]
    assert len(warn2) == 1
    assert warn2[0]["_level"] == "WARNING"
    assert warn2[0]["sustained_intervals"] == 2
    print(f"OK: 2nd preemption = WARNING sustained={warn2[0]['sustained_intervals']}")

    # 3rd preempt: escalates to ERROR (streak=3)
    cap.records.clear()
    mon._evaluate(
        {"vllm:num_preemptions_total": 3.0, "vllm:gpu_cache_usage_perc": 40.0, "vllm:request_success_total": 12.0}
    )
    err3 = [r for r in cap.records if r.get("event") == "vllm_preemption_detected"]
    assert len(err3) == 1
    assert err3[0]["_level"] == "ERROR", f"expected ERROR at streak=3, got {err3[0]['_level']}"
    assert err3[0]["sustained_intervals"] == 3
    assert err3[0]["severity"] == "error"
    print(f"OK: 3rd preemption escalates to ERROR sustained={err3[0]['sustained_intervals']}")


def test_kv_cache_thresholds() -> None:
    """KV-cache usage crosses the warning and critical thresholds."""
    mon, cap = _make_monitor()
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 10.0, "vllm:request_success_total": 5.0}
    )
    cap.records.clear()
    # 82% — above 0.80 warn
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 82.0, "vllm:request_success_total": 7.0}
    )
    high = [r for r in cap.records if r.get("event") == "vllm_kv_cache_high"]
    assert len(high) == 1
    assert high[0]["_level"] == "WARNING"
    assert high[0]["kv_cache_pct"] == 82.0
    print(f"OK: 82% kv cache = WARNING kv={high[0]['kv_cache_pct']}")

    # 96% — above 0.95 critical
    cap.records.clear()
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 96.0, "vllm:request_success_total": 8.0}
    )
    crit = [r for r in cap.records if r.get("event") == "vllm_kv_cache_critical"]
    assert len(crit) == 1
    assert crit[0]["_level"] == "ERROR"
    assert crit[0]["kv_cache_pct"] == 96.0
    print(f"OK: 96% kv cache = ERROR kv={crit[0]['kv_cache_pct']}")

    # Back to 50% — streak resets
    cap.records.clear()
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 50.0, "vllm:request_success_total": 9.0}
    )
    # No high/critical events expected
    assert not any(r.get("event") == "vllm_kv_cache_high" for r in cap.records)
    assert not any(r.get("event") == "vllm_kv_cache_critical" for r in cap.records)
    print("OK: kv cache drop resets streak")


def test_vllm_restart_detection() -> None:
    """A counter reset is reported as a vLLM restart, not a negative delta."""
    mon, cap = _make_monitor()
    mon._evaluate(
        {"vllm:num_preemptions_total": 10.0, "vllm:gpu_cache_usage_perc": 30.0, "vllm:request_success_total": 50.0}
    )
    cap.records.clear()
    # Counter dropped — vLLM restarted
    mon._evaluate(
        {"vllm:num_preemptions_total": 0.0, "vllm:gpu_cache_usage_perc": 5.0, "vllm:request_success_total": 0.0}
    )
    restart = [r for r in cap.records if r.get("event") == "vllm_restart_detected"]
    assert len(restart) == 1
    assert restart[0]["_level"] == "INFO"
    assert restart[0]["preemption_count_before"] == 10
    assert restart[0]["preemption_count_after"] == 0
    print("OK: vLLM restart detected and rebaselined")
    # After restart, next sample should emit a NEW baseline, not a delta event
    cap.records.clear()
    mon._evaluate(
        {"vllm:num_preemptions_total": 2.0, "vllm:gpu_cache_usage_perc": 10.0, "vllm:request_success_total": 3.0}
    )
    # No preemption warning because new baseline = 2, no prior baseline for delta
    assert not any(r.get("event") == "vllm_preemption_detected" for r in cap.records)
    print("OK: post-restart sample is rebaselined silently")


def main() -> None:
    """Run every check and exit non-zero if any fails."""
    test_baseline_event_emitted()
    test_no_event_on_zero_delta_and_low_kv()
    test_preemption_warning_and_escalation()
    test_kv_cache_thresholds()
    test_vllm_restart_detection()
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    main()
