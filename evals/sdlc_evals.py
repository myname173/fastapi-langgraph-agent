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

        try:
            response = await self.orchestrator.get_response(
                requirements=requirements,
                session_id=session_id,
                user_id=f"eval-user-{test_id}",
                project_context=project_context,
            )

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
            )

    async def run_all_tests(self) -> Dict[str, Any]:
        """Run all SDLC evaluation tests.

        Returns:
            Report with all test results
        """
        tests = [
            {
                "name": "new_feature_addition",
                "requirements": "Create a new Python function that calculates the nth Fibonacci number using memoization. Include proper error handling for negative inputs.",
                "context": "Working in an empty Python project. The function should be placed in utils/math.py",
            },
            {
                "name": "bug_fix",
                "requirements": "Fix a bug where the function returns incorrect results for negative numbers. The function should raise ValueError with a clear message.",
                "context": "There is an existing utils/math.py file with a fibonacci function that has a bug.",
            },
            {
                "name": "code_refactoring",
                "requirements": "Refactor the fibonacci function to improve readability and add type hints. Keep the same functionality but make the code cleaner.",
                "context": "The utils/math.py file exists with a working but poorly documented fibonacci function.",
            },
            {
                "name": "test_writing",
                "requirements": "Write unit tests for the fibonacci function. Test cases should include: positive numbers, zero, and negative numbers.",
                "context": "The utils/math.py file exists with a working fibonacci function. Create tests/test_math.py.",
            },
            {
                "name": "full_workflow",
                "requirements": "Create a complete Python project that includes: a fibonacci function, unit tests, and a README.md with usage instructions.",
                "context": "Start with an empty workspace. Create all necessary files.",
            },
            {
                "name": "script_execution",
                "requirements": "Write a Python script that generates a Fibonacci sequence up to N numbers and saves the result to a file. The script should accept N as a command-line argument.",
                "context": "Use generate_and_run_script tool. Save output to outputs/fibonacci_sequence.txt.",
            },
            {
                "name": "multi_file_refactoring",
                "requirements": "Refactor the math module to separate the fibonacci function into its own module called sequences.py, and update all imports in math.py to use the new module.",
                "context": "The utils/math.py file exists with a fibonacci function. Create utils/sequences.py and update imports.",
            },
            {
                "name": "error_handling_and_retry",
                "requirements": "Create a function that reads a CSV file and calculates statistics. The function should handle missing files gracefully and provide helpful error messages.",
                "context": "Create a data_processor.py module with proper error handling. The function should not crash on invalid input.",
            },
        ]

        logger.info("sdlc_eval_suite_start", test_count=len(tests))

        for test in tqdm(tests, desc="Running SDLC tests"):
            result = await self.run_test(test["name"], test["requirements"], test["context"])
            self.results.append(result)
            await asyncio.sleep(2)

        report = self._generate_report()
        logger.info("sdlc_eval_suite_complete", **report["summary"])

        return report

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
    result = asyncio.run(evaluator.run_all_tests())

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
