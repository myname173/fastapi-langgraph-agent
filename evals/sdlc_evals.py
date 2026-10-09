"""SDLC Orchestrator e2e evaluation tests."""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langfuse import Langfuse
from pydantic import BaseModel, Field
from tqdm import tqdm

from app.core.config import settings
from app.core.logging import logger
from app.core.langgraph.graphs.supervisor import SupervisorGraph
from app.core.observability import langfuse_callback_handler


class SDLCEvalResult(BaseModel):
    """Result of an SDLC evaluation test."""

    test_name: str = Field(description="Name of the test")
    test_id: str = Field(description="Unique test ID")
    status: str = Field(description="Test status: passed, failed, or partial")
    steps_completed: int = Field(description="Number of steps completed")
    total_steps: int = Field(description="Total expected steps")
    plan_generated: bool = Field(description="Whether a plan was generated")
    code_changes_made: bool = Field(description="Whether code changes were made")
    tests_executed: bool = Field(description="Whether tests were executed")
    feedback_generated: bool = Field(description="Whether feedback was generated")
    errors: List[str] = Field(default_factory=list, description="List of errors")
    duration_seconds: float = Field(description="Test duration")
    trace_id: Optional[str] = Field(default=None, description="Langfuse trace ID")
    session_id: Optional[str] = Field(default=None, description="Langfuse session ID (fallback linkage)")


class SDLCEvaluator:
    """Evaluator for SDLC Orchestrator e2e tests."""

    def __init__(self):
        """Initialize SDLC evaluator."""
        self.orchestrator = SupervisorGraph()
        self.langfuse = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            timeout=60,
        )
        self.results: List[SDLCEvalResult] = []

    async def run_test(self, test_name: str, requirements: str, project_context: str = "") -> SDLCEvalResult:
        """Run a single SDLC evaluation test.

        Args:
            test_name: Name of the test
            requirements: User requirements for the test
            project_context: Optional project context

        Returns:
            Evaluation result
        """
        test_id = str(uuid.uuid4())[:8]
        session_id = f"sdlc-eval-{test_id}"
        start_time = datetime.now()

        logger.info("sdlc_eval_test_start", test_name=test_name, test_id=test_id)

        trace_id = None
        try:
            response = await self.orchestrator.get_response(
                requirements=requirements,
                session_id=session_id,
                user_id=f"eval-user-{test_id}",
                project_context=project_context,
            )

            # Auto-approve human-in-the-loop gates so the offline eval can run
            # end-to-end without a human. The supervisor raises human_approval_needed
            # (e.g. when PO requests clarification); we approve and resume until the
            # workflow completes or hits a sane limit.
            _approval_budget = 8
            while _approval_budget > 0 and response.get("human_approval_needed", False):
                _approval_budget -= 1
                logger.info(
                    "sdlc_eval_auto_approve",
                    test_name=test_name,
                    test_id=test_id,
                    remaining=_approval_budget,
                )
                response = await self.orchestrator.approve_task(session_id)
            if response.get("human_approval_needed", False):
                logger.warning(
                    "sdlc_eval_approval_budget_exhausted",
                    test_name=test_name,
                    test_id=test_id,
                )

            # L-1 fix: the module-level CallbackHandler has no `trace_id`
            # attribute. Recover the trace via the Langfuse client bound to the
            # handler, keyed by the run's session_id. Falls back to session_id
            # when Langfuse is not reachable (local eval without server).
            if langfuse_callback_handler is not None:
                try:
                    lf_client = getattr(langfuse_callback_handler, "langfuse", None)
                    if lf_client is not None and hasattr(lf_client, "get_trace_id"):
                        trace_id = lf_client.get_trace_id(session_id=session_id)
                except Exception as lf_err:
                    logger.debug("sdlc_eval_trace_fetch_failed", session_id=session_id, error=str(lf_err)[:200])
                if trace_id is None:
                    # No live Langfuse server: keep the session_id as the
                    # deterministic linkage key so traces can be correlated
                    # once a server is configured.
                    trace_id = session_id
                    logger.info("sdlc_eval_trace_fallback_session", session_id=session_id)

            duration = (datetime.now() - start_time).total_seconds()

            plan_generated = len(response.get("plan", [])) > 0
            code_changes_made = len(response.get("code_changes", [])) > 0
            tests_executed = len(response.get("test_results", [])) > 0
            feedback_generated = len(response.get("feedback", [])) > 0
            errors = [e["content"] for e in response.get("errors", [])]

            steps_completed = 0
            if plan_generated:
                steps_completed += 1
            if code_changes_made:
                steps_completed += 1
            if tests_executed:
                steps_completed += 1
            if feedback_generated:
                steps_completed += 1

            total_steps = 4
            status = "passed" if steps_completed == total_steps else "partial"
            if len(errors) > 0:
                status = "failed"

            result = SDLCEvalResult(
                test_name=test_name,
                test_id=test_id,
                status=status,
                steps_completed=steps_completed,
                total_steps=total_steps,
                plan_generated=plan_generated,
                code_changes_made=code_changes_made,
                tests_executed=tests_executed,
                feedback_generated=feedback_generated,
                errors=errors,
                duration_seconds=round(duration, 2),
                trace_id=trace_id,
                session_id=session_id,
            )

            logger.info(
                "sdlc_eval_test_complete",
                test_name=test_name,
                test_id=test_id,
                status=status,
                steps_completed=steps_completed,
                duration_seconds=result.duration_seconds,
            )

            return result

        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            logger.error("sdlc_eval_test_failed", test_name=test_name, test_id=test_id, error=str(e))

            return SDLCEvalResult(
                test_name=test_name,
                test_id=test_id,
                status="failed",
                steps_completed=0,
                total_steps=4,
                plan_generated=False,
                code_changes_made=False,
                tests_executed=False,
                feedback_generated=False,
                errors=[str(e)],
                duration_seconds=round(duration, 2),
                trace_id=trace_id,
                session_id=session_id,
            )

    async def run_all_tests(self) -> Dict[str, Any]:
        """Run all SDLC evaluation tests.

        Returns:
            Report with all test results
        """
        # 真实 Coder Agent 基准任务族（参考 SWE-bench / Terminal-Bench / arxiv 2607.28591）：
        # Bug Fix / Feature Addition / Test Generation / API Migration / Security Repair / CLI Script / Multi-file Refactor
        tests = [
            {
                "name": "bug_fix_off_by_one",
                "requirements": (
                    "修复以下模块中的 off-by-one 缺陷：stats.py 的 mean() 在计算列表长度时未处理空列表，"
                    "且对单元素列表返回 None 而非该元素。要求：空列表返回 0.0，单元素返回该元素，"
                    "负数/非数值输入应抛出 TypeError 并给出清晰信息。给出修复后的 stats.py 并运行验证。"
                ),
                "context": (
                    "现有 stats.py：\n"
                    "def mean(nums):\n"
                    "    total = 0\n"
                    "    for n in nums:\n"
                    "        total += n\n"
                    "    return total / len(nums)\n"
                    "该实现对空列表会 ZeroDivisionError，对单元素列表逻辑正确但无边界保护。"
                ),
            },
            {
                "name": "feature_add_ttl_cache",
                "requirements": (
                    "为现有 HTTP 客户端新增带 TTL 的内存缓存层：相同 URL 在 TTL 内直接返回缓存，"
                    "过期后重新请求。要求线程安全、可配置默认 TTL（默认 60s），并保留原有 GET 接口签名。"
                ),
                "context": (
                    "现有 http_client.py：\n"
                    "import urllib.request\n"
                    "def get(url):\n"
                    "    with urllib.request.urlopen(url) as r:\n"
                    "        return r.read().decode()\n"
                ),
            },
            {
                "name": "test_generation_coverage",
                "requirements": (
                    "为现有 json_parser.py 编写 pytest 单元测试，覆盖：合法 JSON、空对象、嵌套结构、"
                    "非法 JSON（应抛 JSONDecodeError）、以及超大整数。要求测试文件可通过 pytest 运行且全部通过。"
                ),
                "context": ("现有 json_parser.py：\nimport json\ndef parse(text):\n    return json.loads(text)\n"),
            },
            {
                "name": "api_migration_requests_to_httpx",
                "requirements": (
                    "将 fetcher.py 中的 requests 同步调用迁移到 httpx 异步接口（async/await），"
                    "保持 fetch_json(url) 返回解析后的 dict。移除对 requests 的依赖，新增 httpx 依赖说明。"
                ),
                "context": (
                    "现有 fetcher.py：\nimport requests\ndef fetch_json(url):\n    return requests.get(url).json()\n"
                ),
            },
            {
                "name": "security_repair_sqli",
                "requirements": (
                    "修复 db/query.py 中的 SQL 注入隐患：当前使用字符串拼接构造查询。改为参数化查询"
                    "(使用占位符)，并对用户输入做基本校验。说明修复点并给出安全版本代码。"
                ),
                "context": (
                    "现有 db/query.py：\n"
                    "def find_user(conn, username):\n"
                    '    sql = "SELECT * FROM users WHERE name = \'" + username + "\'"\n'
                    "    return conn.execute(sql).fetchall()\n"
                ),
            },
            {
                "name": "cli_script_fibonacci",
                "requirements": (
                    "编写一个 CLI 脚本 fib.py，接受命令行参数 N，生成前 N 个斐波那契数并保存到 outputs/fib.txt，"
                    "同时对 N<=0 给出友好错误并退出码非 0。使用 run_script 工具实际执行验证。"
                ),
                "context": "空 workspace，使用 generate_and_run_script 工具实现并运行。",
            },
            {
                "name": "multi_file_refactor_split",
                "requirements": (
                    "将单体 legacy/tool.py 拆分为 core/calc.py（纯计算函数）与 utils/io.py（读写辅助），"
                    "并更新原 tool.py 改为从新模块导入后转发调用，保持对外接口不变。确保所有导入正确。"
                ),
                "context": ("现有 legacy/tool.py（单文件，含 calculate() 与 save_result()/load_result() 等函数）。"),
            },
        ]

        logger.info("sdlc_eval_suite_start", test_count=len(tests))

        # Allow running a subset of tests (comma-separated names) so different
        # models can be assigned to different tests across separate runs.
        _only = os.getenv("SDLC_EVAL_ONLY")
        if _only:
            _names = {n.strip() for n in _only.split(",") if n.strip()}
            tests = [t for t in tests if t["name"] in _names]
            logger.info("sdlc_eval_subset_filter", only=_only, filtered_count=len(tests))

        # Resume mode: load previously-completed results so a fresh run with a
        # different model can fill in the remaining tests without re-running
        # what already finished. Completed test names are skipped.
        if os.getenv("SDLC_EVAL_RESUME") == "1":
            _prev = self._load_partial_results()
            if _prev:
                self.results.extend(_prev)
                _done = {r.test_name for r in _prev}
                tests = [t for t in tests if t["name"] not in _done]
                logger.info("sdlc_eval_resume", loaded=len(_prev), remaining=len(tests))

        # Per-test timeout so a single stuck test (e.g. model returning a
        # pathological long output that hangs the supervisor graph) cannot
        # block and eventually kill the whole process before any report is
        # written. Raised as asyncio.TimeoutError and caught per-test below.
        _per_test_timeout = float(os.getenv("SDLC_EVAL_TEST_TIMEOUT", "600"))

        # Optional per-test model assignment (JSON map: test_name -> model).
        # Lets a single suite spread tests across models with different quotas
        # / capabilities. Falls back to SDLC_EVAL_DEFAULT_MODEL for any test
        # not present in the map.
        _model_map = {}
        _map_raw = os.getenv("SDLC_EVAL_TEST_MODEL_MAP")
        if _map_raw:
            try:
                _model_map = json.loads(_map_raw)
            except json.JSONDecodeError:
                logger.exception("sdlc_eval_model_map_parse_failed")
        _default_eval_model = os.getenv("SDLC_EVAL_DEFAULT_MODEL")
        if _default_eval_model:
            os.environ["DEFAULT_LLM_MODEL"] = _default_eval_model

        for test in tqdm(tests, desc="Running SDLC tests"):
            # Switch the active model for this test (and rebuild the registry /
            # router fallback chain) so quota / capability can vary per test.
            _tm = _model_map.get(test["name"], _default_eval_model)
            if _tm:
                os.environ["DEFAULT_LLM_MODEL"] = _tm
                try:
                    from app.services.llm import router as _rt, registry as _rg

                    for _role in list(_rt.ROUTING_TABLE.keys()):
                        _rt.ROUTING_TABLE[_role] = _tm
                    _rt.model_router.ROUTING_TABLE = _rt.ROUTING_TABLE
                    _llm = _rg._build_llm(_tm)
                    _rg.LLMRegistry.LLMS = [{"name": _tm, "llm": _llm}]
                except Exception as _me:  # noqa: BLE001
                    logger.exception("sdlc_eval_model_switch_failed", model=_tm, error=str(_me))

            try:
                result = await asyncio.wait_for(
                    self.run_test(test["name"], test["requirements"], test["context"]),
                    timeout=_per_test_timeout,
                )
            except asyncio.TimeoutError:
                # Turn a hung test into a failed result instead of killing the run.
                result = SDLCEvalResult(
                    test_name=test["name"],
                    test_id=test.get("name"),
                    status="failed",
                    steps_completed=0,
                    total_steps=4,
                    plan_generated=False,
                    code_changes_made=False,
                    tests_executed=False,
                    feedback_generated=False,
                    errors=[f"test timed out after {_per_test_timeout}s"],
                    duration_seconds=_per_test_timeout,
                    trace_id=None,
                    session_id=None,
                )
                logger.exception("sdlc_eval_test_timed_out", test_name=test["name"])
            self.results.append(result)
            # Persist a partial report after EVERY test so a crash mid-suite
            # still leaves a readable JSON with all completed results.
            self._write_partial_report()
            await asyncio.sleep(2)

        report = self._generate_report()
        logger.info("sdlc_eval_suite_complete", **report["summary"])

        return report

    def _write_partial_report(self) -> None:
        """Write an intermediate report with results collected so far.

        Called after every test so that if the process dies before the full
        suite finishes, the already-completed results are not lost.
        """
        if not self.results:
            return
        passed = sum(1 for r in self.results if r.status == "passed")
        partial = sum(1 for r in self.results if r.status == "partial")
        failed = sum(1 for r in self.results if r.status == "failed")
        total = len(self.results)
        report = {
            "timestamp": datetime.now().isoformat(),
            "partial": True,
            "summary": {
                "total_tests": total,
                "passed": passed,
                "partial": partial,
                "failed": failed,
                "pass_rate": round((passed / total) * 100, 2) if total > 0 else 0,
                "avg_duration_seconds": round(sum(r.duration_seconds for r in self.results) / total, 2)
                if total > 0
                else 0,
                "avg_steps_completed": round(sum(r.steps_completed for r in self.results) / total, 2)
                if total > 0
                else 0,
                "langfuse_trace_ids": [r.trace_id for r in self.results if r.trace_id],
                "session_ids": [r.session_id for r in self.results if r.session_id],
            },
            "tests": [r.model_dump() for r in self.results],
        }
        report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, "sdlc_eval_partial.json")
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
        except OSError:
            logger.exception("sdlc_eval_partial_write_failed")

    def _load_partial_results(self) -> List[SDLCEvalResult]:
        """Load already-completed results from the partial report for resume mode."""
        report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        report_path = os.path.join(report_dir, "sdlc_eval_partial.json")
        if not os.path.exists(report_path):
            return []
        try:
            with open(report_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            logger.exception("sdlc_eval_partial_read_failed")
            return []
        loaded: List[SDLCEvalResult] = []
        for t in data.get("tests", []):
            try:
                loaded.append(SDLCEvalResult(**t))
            except Exception:  # noqa: BLE001 - skip malformed entries
                logger.exception("sdlc_eval_partial_entry_skipped", test=t.get("test_name"))
        return loaded

    def _generate_report(self) -> Dict[str, Any]:
        """Generate evaluation report.

        Returns:
            Report dictionary
        """
        passed = sum(1 for r in self.results if r.status == "passed")
        partial = sum(1 for r in self.results if r.status == "partial")
        failed = sum(1 for r in self.results if r.status == "failed")
        total = len(self.results)

        avg_duration = sum(r.duration_seconds for r in self.results) / total if total > 0 else 0
        avg_steps = sum(r.steps_completed for r in self.results) / total if total > 0 else 0

        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "total_tests": total,
                "passed": passed,
                "partial": partial,
                "failed": failed,
                "pass_rate": round((passed / total) * 100, 2) if total > 0 else 0,
                "avg_duration_seconds": round(avg_duration, 2),
                "avg_steps_completed": round(avg_steps, 2),
                "langfuse_trace_ids": [r.trace_id for r in self.results if r.trace_id],
                "session_ids": [r.session_id for r in self.results if r.session_id],
            },
            "tests": [r.model_dump() for r in self.results],
        }

        report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(report_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(report_dir, f"sdlc_eval_report_{timestamp}.json")

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        report["report_path"] = report_path
        logger.info("sdlc_eval_report_generated", report_path=report_path)

        return report


if __name__ == "__main__":
    evaluator = SDLCEvaluator()
    # 本地评测（无 Docker PG 或 Windows 异步连接受限）时降级为内存 checkpointer，
    # 保证 SDLC 流程与 LLM 调用、Langfuse 追踪、评测报告仍可正常产出。
    os.environ.setdefault("SDLC_EVAL_LOCAL", "1")
    # Windows 上 psycopg 异步模式不兼容 ProactorEventLoop，需改用 SelectorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop_factory = asyncio.SelectorEventLoop
    else:
        loop_factory = asyncio.new_event_loop
    result = asyncio.run(evaluator.run_all_tests(), loop_factory=loop_factory)

    print("\n" + "=" * 60)
    print("SDLC E2E Evaluation Report")
    print("=" * 60)
    print(f"Total Tests: {result['summary']['total_tests']}")
    print(f"Passed: {result['summary']['passed']}")
    print(f"Partial: {result['summary']['partial']}")
    print(f"Failed: {result['summary']['failed']}")
    print(f"Pass Rate: {result['summary']['pass_rate']}%")
    print(f"Avg Duration: {result['summary']['avg_duration_seconds']}s")
    print(f"Report saved to: {result['report_path']}")
    print("=" * 60)
