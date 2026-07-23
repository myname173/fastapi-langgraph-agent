"""Golden dataset for trajectory-level evaluation.

Single source of truth for expected agent trajectories. Consumed by:

- ``tests/test_eval_regression.py`` — CI regression gate (deterministic,
  mock LLM, zero token cost). Imports ``GOLDEN_TASKS`` directly.
- ``mcp_server/seed_db.py`` — populates the sqlite ``eval_golden`` table
  (exposed at runtime via the MCP ``query_eval_golden`` tool) by importing
  this module via ``sys.path`` append.
- ``evals/evaluator.py`` — ``TrajectoryEvaluator`` uses the rubrics (not
  the goldens directly) to score real Langfuse traces.

See ``docs/eval-upgrade.md`` for the full design.
"""

from evals.golden.tasks import (
    GOLDEN_TASKS,
    GoldenTask,
)

__all__ = ["GOLDEN_TASKS", "GoldenTask"]
