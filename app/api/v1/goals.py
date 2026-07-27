"""Goal API endpoints for cross-session long-term objective tracking."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.v1.auth import get_current_user
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.user import User
from app.schemas.goal import (
    GoalCreate,
    GoalDetailResponse,
    GoalResponse,
    GoalUpdate,
    TaskAssociateRequest,
)
from app.services.goal_service import goal_service
from app.utils.errors import safe_500

router = APIRouter()


def _build_task_summaries(tasks) -> List[dict]:
    """Convert SDLCTask list to API-friendly summaries."""
    return [
        {"id": t.id, "goal_id": t.goal_id, "status": t.status.value, "requirements": t.requirements[:80]}
        for t in tasks
    ]


async def _build_goal_detail(goal_id: str) -> GoalDetailResponse:
    """Build a GoalDetailResponse with tasks and progress.

    Caller must verify the goal exists before invoking this helper.
    """
    goal = await goal_service.get_goal(goal_id)
    assert goal is not None  # caller verifies existence + ownership
    tasks = await goal_service.get_tasks_by_goal(goal_id)
    progress = await goal_service.get_progress(goal_id)
    return GoalDetailResponse(
        **goal.model_dump(),
        tasks=_build_task_summaries(tasks),
        progress=progress,
    )


@router.post("/", response_model=GoalResponse)
@limiter.limit("10 per minute")
async def create_goal(request: Request, goal_data: GoalCreate, user: User = Depends(get_current_user)):
    """Create a new long-term goal."""
    try:
        goal_id = str(uuid.uuid4())
        goal = await goal_service.create_goal(
            goal_id=goal_id,
            user_id=user.id,
            title=goal_data.title,
            description=goal_data.description,
        )
        logger.info("goal_created_via_api", goal_id=goal_id, user_id=user.id)
        return goal
    except Exception as e:
        raise safe_500(e, context="goal_create")


@router.get("/", response_model=List[GoalResponse])
async def list_goals(request: Request, user: User = Depends(get_current_user)):
    """List all goals for the current user."""
    try:
        goals = await goal_service.get_goals_by_user(user.id)
        return goals
    except Exception as e:
        raise safe_500(e, context="goal_list")


@router.get("/{goal_id}", response_model=GoalDetailResponse)
async def get_goal(goal_id: str, request: Request, user: User = Depends(get_current_user)):
    """Get goal details with associated tasks and progress."""
    try:
        goal = await goal_service.get_goal(goal_id)
        if not goal or goal.user_id != user.id:
            raise HTTPException(status_code=404, detail="Goal not found")
        return await _build_goal_detail(goal_id)
    except HTTPException:
        raise
    except Exception as e:
        raise safe_500(e, context="goal_detail")


@router.patch("/{goal_id}", response_model=GoalResponse)
async def update_goal(
    goal_id: str,
    goal_data: GoalUpdate,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Update goal title/description/status."""
    try:
        goal = await goal_service.get_goal(goal_id)
        if not goal or goal.user_id != user.id:
            raise HTTPException(status_code=404, detail="Goal not found")

        updated = await goal_service.update_goal(
            goal_id,
            title=goal_data.title,
            description=goal_data.description,
            status=goal_data.status,
        )
        return updated
    except HTTPException:
        raise
    except Exception as e:
        raise safe_500(e, context="goal_update")


@router.delete("/{goal_id}")
async def delete_goal(goal_id: str, request: Request, user: User = Depends(get_current_user)):
    """Delete a goal. Associated tasks are preserved (goal_id set to None)."""
    try:
        goal = await goal_service.get_goal(goal_id)
        if not goal or goal.user_id != user.id:
            raise HTTPException(status_code=404, detail="Goal not found")

        # Dissociate all tasks before deleting (preserve task history)
        tasks = await goal_service.get_tasks_by_goal(goal_id)
        for task in tasks:
            await goal_service.dissociate_task(task.id)

        await goal_service.delete_goal(goal_id)
        logger.info("goal_deleted_via_api", goal_id=goal_id, user_id=user.id)
        return {"message": "Goal deleted", "goal_id": goal_id}
    except HTTPException:
        raise
    except Exception as e:
        raise safe_500(e, context="goal_delete")


@router.post("/{goal_id}/tasks", response_model=GoalDetailResponse)
async def associate_task_to_goal(
    goal_id: str,
    body: TaskAssociateRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Associate an existing SDLC task with this goal."""
    try:
        goal = await goal_service.get_goal(goal_id)
        if not goal or goal.user_id != user.id:
            raise HTTPException(status_code=404, detail="Goal not found")

        task = await goal_service.associate_task(goal_id, body.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        return await _build_goal_detail(goal_id)
    except HTTPException:
        raise
    except Exception as e:
        raise safe_500(e, context="goal_associate_task")


@router.delete("/{goal_id}/tasks/{task_id}")
async def remove_task_from_goal(
    goal_id: str,
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Remove a task's association with this goal."""
    try:
        goal = await goal_service.get_goal(goal_id)
        if not goal or goal.user_id != user.id:
            raise HTTPException(status_code=404, detail="Goal not found")

        await goal_service.dissociate_task(task_id)
        return {
            "message": "Task removed from goal",
            "goal_id": goal_id,
            "task_id": task_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise safe_500(e, context="goal_dissociate_task")
