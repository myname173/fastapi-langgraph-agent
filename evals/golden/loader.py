"""Loaders for the golden dataset.

Thin accessors over ``evals.golden.tasks.GOLDEN_TASKS``. Kept separate from
``tasks.py`` so data definition and data access stay decoupled — callers
depend on the loader, not the raw list.
"""

from __future__ import annotations

from evals.golden.tasks import GOLDEN_TASKS, GoldenTask


def load_golden_tasks() -> list[GoldenTask]:
    """Return all golden tasks.

    Returns:
        A copy-free reference to the module-level ``GOLDEN_TASKS`` list.
        Callers must not mutate it.
    """
    return GOLDEN_TASKS


def get_golden_by_type(task_type: str) -> GoldenTask | None:
    """Look up a golden task by its ``task_type`` identifier.

    Args:
        task_type: The ``task_type`` field to match.

    Returns:
        The matching :class:`GoldenTask`, or ``None`` if no task has the
        given type.
    """
    for task in GOLDEN_TASKS:
        if task.task_type == task_type:
            return task
    return None
