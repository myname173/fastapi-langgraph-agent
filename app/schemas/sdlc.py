"""This file contains the SDLC Orchestrator Pydantic models for API validation.

These models are used for:
- Validating API request/response data
- Structuring agent outputs (with structured output format)
- Converting between internal dict state and external API formats
"""

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Task(BaseModel):
    """Represents a single task in the SDLC execution plan."""

    id: str = Field(..., description="任务唯一标识")
    description: str = Field(..., description="任务描述")
    status: Literal["pending", "in_progress", "completed"] = Field(default="pending")
    assignee: Optional[str] = Field(None, description="负责代理名称")


class FileChange(BaseModel):
    """Represents a change to a single file."""

    file_path: str = Field(..., description="文件路径")
    change_type: Literal["create", "modify", "delete"] = Field(..., description="变更类型")
    content: str = Field(..., description="变更内容")
    commit_message: Optional[str] = Field(None, description="提交信息")


class CodeEdit(BaseModel):
    """Represents a single code edit operation for smart editing."""

    file_path: str = Field(..., description="目标文件路径")
    edit_type: Literal["replace", "insert", "create", "delete"] = Field(
        ..., description="编辑类型：replace=替换旧文本, insert=在指定位置后插入, create=新建文件, delete=删除文件"
    )
    old_str: Optional[str] = Field(None, description="要替换的旧内容（replace 时必填）")
    new_str: str = Field(..., description="新内容")
    reason: Optional[str] = Field(None, description="编辑原因说明")


class CoderPlanStep(BaseModel):
    """A single step in the coder's implementation plan."""

    step: int = Field(..., description="步骤编号")
    description: str = Field(..., description="步骤描述")
    file_path: Optional[str] = Field(None, description="涉及的文件")
    reasoning: Optional[str] = Field(None, description="为什么要做这一步")
    depends_on: List[int] = Field(default_factory=list, description="依赖的前置步骤编号")

    @field_validator("depends_on", mode="before")
    @classmethod
    def _coerce_depends_on(cls, v):
        """Coerce depends_on entries to int.

        LLMs frequently output ``["step 1", "step 2"]`` instead of ``[1, 2]``.
        Extract the integer from each entry to avoid schema validation
        failures that would derail the coder workflow (parse failure →
        fallback → empty workspace → infinite coder loop).
        """
        if not v:
            return []
        result: list[int] = []
        for item in v:
            if isinstance(item, bool):
                # bool is a subclass of int; skip to avoid False→0 coercion
                continue
            if isinstance(item, int):
                result.append(item)
            elif isinstance(item, str):
                match = re.search(r"\d+", item)
                if match:
                    result.append(int(match.group()))
            # else: skip unsupported types (None, dict, list, etc.)
        return result


class TestResult(BaseModel):
    """Represents the result of a single test execution."""

    test_name: str = Field(..., description="测试名称")
    status: Literal["pass", "fail", "skip"] = Field(..., description="测试状态")
    error_message: Optional[str] = Field(None, description="错误信息")
    duration_ms: Optional[int] = Field(None, description="执行时长（毫秒）")


class FeedbackItem(BaseModel):
    """Represents feedback from a reviewer agent."""

    agent_name: str = Field(..., description="反馈来源代理名称")
    content: str = Field(..., description="反馈内容")
    severity: Literal["low", "medium", "high", "critical"] = Field(default="medium")


class SupervisorDecision(BaseModel):
    """Structured output for supervisor routing decisions."""

    next_agent: Literal["planner", "coder", "reviewer", "tester", "END"] = Field(..., description="下一个要调用的代理")
    reasoning: str = Field(..., description="决策理由")
    status_update: Optional[str] = Field(None, description="状态更新信息")
    is_complete: bool = Field(default=False, description="流程是否完成")


class PlannerOutput(BaseModel):
    """Structured output for the planner agent."""

    plan: List[Task] = Field(default_factory=list, description="执行计划任务列表")
    summary: str = Field(default="", description="计划摘要")


class CoderOutput(BaseModel):
    """Structured output for the coder agent."""

    plan: List[CoderPlanStep] = Field(default_factory=list, description="实施计划步骤")
    edits: List[CodeEdit] = Field(default_factory=list, description="智能编辑操作列表")
    code_changes: List[FileChange] = Field(default_factory=list, description="代码变更列表（兼容旧格式）")
    needs_human_approval: bool = Field(default=False, description="是否需要人工审批")
    approval_reason: Optional[str] = Field(None, description="需要审批的原因")
    impact_report: Optional[str] = Field(None, description="变更影响报告")
    summary: str = Field(default="", description="编码摘要")


class TesterOutput(BaseModel):
    """Structured output for the tester agent."""

    test_results: List[TestResult] = Field(default_factory=list, description="测试结果列表")
    summary: str = Field(default="", description="测试摘要")


class SecurityIssue(BaseModel):
    """Represents a security issue found during scanning."""

    severity: Literal["low", "medium", "high", "critical"] = Field(default="medium")
    message: str = Field(..., description="问题描述")
    match: str = Field(default="", description="匹配内容")
    context: str = Field(default="", description="上下文")
    file: Optional[str] = Field(None, description="文件路径")


class ReviewerOutput(BaseModel):
    """Structured output for the reviewer agent."""

    feedback: List[FeedbackItem] = Field(default_factory=list, description="审查反馈列表")
    security_issues: List[SecurityIssue] = Field(default_factory=list, description="安全问题列表")
    summary: str = Field(default="", description="审查摘要")


class SDLCRequest(BaseModel):
    """Request model for SDLC Orchestrator API."""

    requirements: str = Field(..., description="用户需求描述")
    project_context: Optional[str] = Field(None, description="项目上下文信息")
    images: Optional[List[str]] = Field(None, description="图片列表（base64 编码或 URL）")
    coder_model: Optional[str] = Field(
        None, description="用户指定的 coder 模型（须在 GET /sdlc/models 返回的可选列表内）"
    )
    goal_id: Optional[str] = Field(None, description="关联的 Goal ID，用于跨会话上下文回灌")


class SDLCResponse(BaseModel):
    """Response model for SDLC Orchestrator API."""

    status: Literal["planning", "coding", "testing", "reviewing", "completed"] = Field(..., description="当前阶段状态")
    plan: List[Task] = Field(default_factory=list, description="执行计划")
    code_changes: List[FileChange] = Field(default_factory=list, description="代码变更")
    test_results: List[TestResult] = Field(default_factory=list, description="测试结果")
    feedback: List[FeedbackItem] = Field(default_factory=list, description="审查反馈")
    security_issues: List[SecurityIssue] = Field(default_factory=list, description="安全问题")
    is_complete: bool = Field(default=False, description="流程是否完成")
    message: str = Field(default="", description="状态消息")
    human_approval_needed: bool = Field(default=False, description="是否需要人工确认")
    # L-3 fix: unique checkpoint thread ID per orchestration call. Previously
    # thread_id was always session_id, so concurrent tasks in the same session
    # overwrote each other's checkpoint. Now each call gets a fresh UUID;
    # the UI must capture this and use it for /state, /approve, /reject.
    thread_id: str = Field(default="", description="本次编排的 checkpoint 线程 ID")


class SDLCReplayResponse(SDLCResponse):
    """Response model for SDLC replay endpoint.

    Extends SDLCResponse with the new thread_id generated for the replayed
    task. The client uses this thread_id for subsequent approve/reject/stream
    calls on the new branch.
    """

    new_thread_id: str = Field(..., description="新分支的 thread_id，用于后续 approve/reject/stream 调用")


class ImageAnalysisResult(BaseModel):
    """Result of image analysis by Vision LLM."""

    image_id: str = Field(..., description="图片唯一标识")
    description: str = Field(..., description="图片内容描述")
    key_elements: List[str] = Field(default_factory=list, description="图片中识别的关键元素")
    suggestions: List[str] = Field(default_factory=list, description="基于图片内容的建议")
