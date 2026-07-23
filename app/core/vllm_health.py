"""vLLM service health monitor.

Background poller that scrapes Prometheus metrics from the local vLLM
container (``http://vllm:8001/metrics``) and emits structured log events
when memory-pressure signals cross configured thresholds. Designed to give
operators early warning of OOM risk on the single-user GTX 1650 Ti (4GB)
deployment described in ``docs/vllm-optimization.md``.

Monitored signals (vLLM 0.8.5 Prometheus exporter):

- ``vllm:num_preemptions_total`` (counter) — KV cache evictions. Any
  positive delta since the previous sample indicates a request was
  preempted to make room for another. **Most direct OOM risk indicator.**
- ``vllm:gpu_cache_usage_perc`` (gauge) — fraction of the GPU KV cache
  currently in use. Sustained > 85% suggests headroom is shrinking; > 95%
  is a near-certain OOM if a long-context request arrives.
- ``vllm:request_success_total`` (counter) — total successful requests
  served. Tracked to detect vLLM restarts (counter resets to 0) and to
  compute time-between-samples throughput.

The monitor runs as an ``asyncio`` task scheduled by ``app.main.lifespan``
via ``task_manager.schedule`` and is cleanly cancelled during shutdown.

Failure-mode policy:

- vLLM is an **optional** dependency (app falls back to cloud models on
  any LLM error). HTTP errors reaching the metrics endpoint are logged
  at WARNING (not ERROR) and never raised, so a vLLM outage cannot take
  down the app.
- All thresholds are configurable via ``.env``. The defaults are
  conservative for a 4GB-VRAM single-user box (warning at first
  preemption, critical at > 95% KV cache usage).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.core.task_manager import task_manager


# Reused from test_data/vllm_metrics.py — the metric name is the public
# contract of vLLM's Prometheus exporter, so the same regex applies here.
_METRIC_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>[-+0-9.eE+nNaA]+)"
)


class VLLMHealthMonitor:
    """Periodically scrapes vLLM Prometheus metrics and logs OOM-risk events.

    The monitor is **stateful** between samples: it tracks the last seen
    preemption counter so it can emit an event with ``delta`` = number of
    preemptions that occurred during the most recent interval. It also
    detects vLLM restarts (counter dropping below the last seen value) and
    silently rebaselines.

    Lifecycle (called from ``app.main.lifespan``):

    >>> monitor = VLLMHealthMonitor()
    >>> await monitor.start()    # schedules background task
    >>> # ... app runs ...
    >>> await monitor.stop()     # cancels task and awaits final sample
    """

    def __init__(self) -> None:
        """Initialize the monitor with config-derived parameters."""
        self._url = settings.VLLM_METRICS_URL
        self._interval = settings.VLLM_HEALTH_CHECK_INTERVAL_SECONDS
        self._timeout = settings.VLLM_HEALTH_CHECK_TIMEOUT_SECONDS
        self._preempt_warn = settings.VLLM_PREEMPTION_WARN_THRESHOLD
        self._kv_warn = settings.VLLM_KV_CACHE_WARN_THRESHOLD
        self._kv_critical = settings.VLLM_KV_CACHE_CRITICAL_THRESHOLD
        self._task: Optional[asyncio.Task[None]] = None
        # Baseline counters — None means "not yet observed".
        self._last_preemptions: Optional[float] = None
        self._last_request_count: Optional[float] = None
        # Track sustained-threshold violations to escalate log level.
        self._kv_warn_streak: int = 0
        self._preempt_streak: int = 0

    async def start(self) -> None:
        """Schedule the monitor as a tracked background task.

        The task is registered with ``task_manager`` so it is cleanly
        cancelled during application shutdown. A single sample is taken
        immediately on start so the first structured log line shows the
        baseline state.
        """
        if self._task is not None and not self._task.done():
            logger.warning("vllm_health_monitor_already_running")
            return
        logger.info(
            "vllm_health_monitor_started",
            url=self._url,
            interval_seconds=self._interval,
            preemption_warn_threshold=self._preempt_warn,
            kv_cache_warn_threshold=self._kv_warn,
            kv_cache_critical_threshold=self._kv_critical,
        )
        self._task = await task_manager.schedule(self._run_loop(), name="vllm_health_monitor")

    async def stop(self) -> None:
        """Cancel the background task and wait for it to unwind.

        Called from ``app.main.lifespan`` on shutdown. Idempotent.
        """
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("vllm_health_monitor_stopped")

    async def _run_loop(self) -> None:
        """Main polling loop. Catches all exceptions so a single failure
        does not kill the monitor permanently — we want a transient
        network blip to be reported once, not to silently disable the
        whole alert.
        """
        # Take an immediate sample so the first log line is the baseline
        # rather than waiting `interval` seconds.
        await self._sample_once()
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            try:
                await self._sample_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.exception(
                    "vllm_health_sample_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    async def _sample_once(self) -> None:
        """Fetch metrics, parse, evaluate thresholds, emit log events."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._url)
        except httpx.HTTPError as exc:
            # vLLM is optional; a connection error is a WARNING, not
            # an error that should page anyone. App continues to serve
            # via cloud fallback.
            logger.warning(
                "vllm_metrics_unreachable",
                url=self._url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        if resp.status_code != 200:
            logger.warning(
                "vllm_metrics_non_200",
                url=self._url,
                status_code=resp.status_code,
                body_preview=resp.text[:200],
            )
            return
        metrics = _parse_prometheus(resp.text)
        self._evaluate(metrics)

    def _evaluate(self, metrics: Dict[str, float]) -> list[Dict[str, Any]]:
        """Compare current sample against baselines and thresholds.

        Counter semantics: ``num_preemptions_total`` is monotonic per
        vLLM process. If the new value is less than the last seen value,
        vLLM was restarted — we re-baseline silently and emit an info
        event so operators notice the restart in the logs.

        Returns:
            A list of structured event dicts that were emitted during
            this sample. Useful for unit tests that want to assert on
            event content without standing up a structlog handler.
        """
        preemptions = metrics.get("vllm:num_preemptions_total", 0.0)
        kv_cache_pct = metrics.get("vllm:gpu_cache_usage_perc", 0.0)
        request_count = metrics.get("vllm:request_success_total", 0.0)
        events: list[Dict[str, Any]] = []

        # ---- vLLM restart detection (counter wrap) -----------------------
        if self._last_preemptions is not None and preemptions < self._last_preemptions:
            event = {
                "event": "vllm_restart_detected",
                "level": "info",
                "preemption_count_before": int(self._last_preemptions),
                "preemption_count_after": int(preemptions),
                "request_count_before": (
                    int(self._last_request_count) if self._last_request_count is not None else None
                ),
                "request_count_after": int(request_count),
            }
            events.append(event)
            logger.info(**event)
            self._last_preemptions = None
            self._last_request_count = None
            self._preempt_streak = 0
            self._kv_warn_streak = 0

        # ---- preemption delta evaluation ---------------------------------
        if self._last_preemptions is None:
            self._last_preemptions = preemptions
            self._last_request_count = request_count
            event = {
                "event": "vllm_health_baseline",
                "level": "info",
                "preemptions": int(preemptions),
                "kv_cache_pct": round(kv_cache_pct, 2),
                "requests_served": int(request_count),
            }
            events.append(event)
            logger.info(**event)
            return events

        preempt_delta = preemptions - self._last_preemptions
        self._last_preemptions = preemptions
        self._last_request_count = request_count

        if preempt_delta >= self._preempt_warn:
            self._preempt_streak += 1
            level = "error" if self._preempt_streak >= 3 else "warning"
            event = {
                "event": "vllm_preemption_detected",
                "level": level,
                "delta": int(preempt_delta),
                "total": int(preemptions),
                "kv_cache_pct": round(kv_cache_pct, 2),
                "interval_seconds": self._interval,
                "sustained_intervals": self._preempt_streak,
                "severity": level,
            }
            events.append(event)
            if level == "error":
                logger.error(**event)
            else:
                logger.warning(**event)
        else:
            self._preempt_streak = 0

        # ---- KV cache pressure evaluation --------------------------------
        kv_pct_fraction = kv_cache_pct / 100.0
        if kv_pct_fraction >= self._kv_critical:
            self._kv_warn_streak += 1
            event = {
                "event": "vllm_kv_cache_critical",
                "level": "error",
                "kv_cache_pct": round(kv_cache_pct, 2),
                "threshold_pct": round(self._kv_critical * 100, 2),
                "sustained_intervals": self._kv_warn_streak,
            }
            events.append(event)
            logger.error(**event)
        elif kv_pct_fraction >= self._kv_warn:
            self._kv_warn_streak += 1
            # Only log on the first crossing and every 5th sustained
            # interval to avoid log spam during a long pressure period.
            if self._kv_warn_streak == 1 or self._kv_warn_streak % 5 == 0:
                event = {
                    "event": "vllm_kv_cache_high",
                    "level": "warning",
                    "kv_cache_pct": round(kv_cache_pct, 2),
                    "threshold_pct": round(self._kv_warn * 100, 2),
                    "sustained_intervals": self._kv_warn_streak,
                }
                events.append(event)
                logger.warning(**event)
        else:
            self._kv_warn_streak = 0

        # Always log a debug summary so engineers can see the steady
        # state in dev logs without it being noisy at INFO level.
        logger.debug(
            "vllm_health_sample",
            preemptions=int(preemptions),
            preempt_delta=int(preempt_delta),
            kv_cache_pct=round(kv_cache_pct, 2),
            requests_served=int(request_count),
        )
        return events


def _parse_prometheus(text: str) -> Dict[str, float]:
    """Parse a Prometheus text-exposition response into a flat name→value map.

    Counter-style metrics (``*_total``) may have multiple samples with
    different label sets. We sum them per the Prometheus convention that
    counters are aggregated across labels. This matches what Grafana
    does by default.

    Args:
        text: Raw Prometheus text exposition response body.

    Returns:
        Mapping of metric name to its summed value across all label
        sets. Gauges with multiple label sets are summed too — for
        ``gpu_cache_usage_perc`` there is only one sample, so this is
        a no-op in practice, but it is safer than silently dropping
        samples.
    """
    out: Dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_LINE.match(line)
        if not m:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        name = m.group("name")
        out[name] = out.get(name, 0.0) + value
    return out


# Module-level singleton — imported by ``app.main.lifespan``.
vllm_health_monitor = VLLMHealthMonitor()
