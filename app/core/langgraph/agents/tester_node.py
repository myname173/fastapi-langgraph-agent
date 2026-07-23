"""Tester node — executes tests and reports results.

Extracted from the former ``SupervisorGraph._tester_node`` method.
"""

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import Command

from app.core.langgraph.agents.context import (
    AgentContext,
    build_error_command,
    get_node_metadata,
)
from app.core.langgraph.agents.supervisor_node import NodeCallable
from app.core.logging import logger
from app.core.metrics import mcp_tool_calls_total, sdlc_agent_execution_seconds
from app.core.prompts import load_agent_prompt
from app.schemas import SDLCState, TesterOutput
from app.services.llm.usage_tracker import set_llm_context
from app.utils import prepare_sdlc_messages


def make_tester_node(ctx: AgentContext) -> NodeCallable:
    """Create the tester node function."""

    async def tester_node(state: SDLCState, config: RunnableConfig) -> Command:
        system_prompt = load_agent_prompt(
            "tester",
            requirements=state.get("requirements", ""),
            plan=state.get("plan", []),
            code_changes=state.get("code_changes", []),
        )
        messages = prepare_sdlc_messages(state["messages"], system_prompt)

        thread_id = config.get("configurable", {}).get("thread_id", "default")

        test_tools = ctx.registry.get_for_agent("tester")
        tools_by_name = {t.name: t for t in test_tools}

        llm_with_tools = ctx.llm_service.get_llm_for(ctx.model_router.get_model_for("tester")).bind_tools(test_tools)

        try:
            with (
                sdlc_agent_execution_seconds.labels(agent_name="tester", step="testing", status="success").time(),
                set_llm_context("tester", "testing"),
            ):
                response = await llm_with_tools.ainvoke(
                    messages,
                    config={"metadata": get_node_metadata("tester", "testing", success=True)},
                )

            if response.tool_calls:
                # Execute tool calls directly instead of via ToolNode.
                # ToolNode in newer LangGraph versions requires a runtime
                # config key ("__pregel_runtime") that is only injected when
                # called from within a graph node — direct ainvoke() does
                # not have it, causing "Missing required config key 'N/A'
                # for 'tools'" errors.
                tool_messages: list[ToolMessage] = []
                test_output_parts: list[str] = []
                for tool_call in response.tool_calls:
                    tool_name = tool_call["name"]
                    args = tool_call.get("args", {})
                    if tool_name in tools_by_name:
                        try:
                            result = await tools_by_name[tool_name].ainvoke(args, config=config)
                            result_str = str(result)
                            test_output_parts.append(result_str)
                            tool_messages.append(ToolMessage(content=result_str, tool_call_id=tool_call["id"]))
                            # MCP tool invocation metric — success path.
                            if tool_name.startswith("mcp_"):
                                mcp_tool_calls_total.labels(tool=tool_name, status="success").inc()
                        except Exception as tool_err:
                            err_msg = f"Tool {tool_name} failed: {tool_err}"
                            test_output_parts.append(err_msg)
                            tool_messages.append(ToolMessage(content=err_msg, tool_call_id=tool_call["id"]))
                            # MCP tool invocation metric — failure path.
                            if tool_name.startswith("mcp_"):
                                mcp_tool_calls_total.labels(tool=tool_name, status="failed").inc()
                    else:
                        err_msg = f"Unknown tool: {tool_name}"
                        test_output_parts.append(err_msg)
                        tool_messages.append(ToolMessage(content=err_msg, tool_call_id=tool_call["id"]))
                        # Defensive: unknown mcp_* tool means registry desync.
                        if tool_name.startswith("mcp_"):
                            mcp_tool_calls_total.labels(tool=tool_name, status="failed").inc()

                # Auto-invoke run_tests if the LLM didn't call it. The tester
                # LLM frequently calls list_directory/search_code to inspect
                # the workspace but forgets to actually run tests, leaving
                # test_output without "Return code: 0" → perpetual fail →
                # supervisor loops back to tester forever (no circuit breaker
                # for tester). Forcing run_tests here guarantees test
                # execution regardless of LLM tool selection.
                run_tests_called = any(tc["name"] == "run_tests" for tc in response.tool_calls)
                if not run_tests_called and "run_tests" in tools_by_name:
                    try:
                        run_tests_tool = tools_by_name["run_tests"]
                        auto_result = await run_tests_tool.ainvoke(
                            {"thread_id": thread_id, "test_command": "pytest -v", "path": "."},
                            config=config,
                        )
                        auto_result_str = str(auto_result)
                        test_output_parts.append(auto_result_str)
                        tool_messages.append(ToolMessage(content=auto_result_str, tool_call_id="auto_run_tests"))
                        logger.info("sdlc_tester_auto_run_tests", triggered=True)
                    except Exception as auto_err:
                        err_msg = f"Auto run_tests failed: {auto_err}"
                        test_output_parts.append(err_msg)
                        tool_messages.append(ToolMessage(content=err_msg, tool_call_id="auto_run_tests"))
                        logger.error("sdlc_tester_auto_run_tests_failed", error=str(auto_err))

                test_output = "\n".join(test_output_parts)
                test_results = []
                if "Return code: 0" in test_output:
                    test_results.append({"test_name": "all_tests", "status": "pass", "output": test_output[:1000]})
                else:
                    test_results.append({"test_name": "all_tests", "status": "fail", "output": test_output[:1000]})

                logger.info("sdlc_tester_tool_calls", test_status=test_results[0]["status"])

                # Append an error entry on test failure so the supervisor's
                # existing circuit breaker (counts errors per agent; trips
                # when count > MAX_CODER_RETRIES) can terminate the loop.
                # Without this, tester failure doesn't increment errors and
                # supervisor re-routes to tester indefinitely.
                update_dict: dict = {
                    "messages": [response] + tool_messages,
                    "test_results": test_results,
                    "status": "testing",
                }
                if test_results[0]["status"] == "fail":
                    update_dict["errors"] = state.get("errors", []) + [
                        {
                            "agent_name": "tester",
                            "content": f"Tests failed (output: {test_output[:200]})",
                            "severity": "medium",
                        }
                    ]

                return Command(
                    update=update_dict,
                    goto="supervisor",
                )
            else:
                try:
                    parsed = TesterOutput.model_validate_json(response.content)
                    test_results_data = (
                        [result.model_dump() for result in parsed.test_results] if parsed.test_results else []
                    )
                except Exception:
                    test_results_data = []

                logger.info(
                    "sdlc_tester_completed",
                    test_count=len(test_results_data),
                    passed_count=sum(1 for r in test_results_data if r["status"] == "pass"),
                )

                return Command(
                    update={
                        "messages": [{"role": "assistant", "content": response.content}],
                        "test_results": test_results_data,
                        "status": "testing",
                    },
                    goto="supervisor",
                )
        except Exception as e:
            return build_error_command("tester", "testing", "testing", e)

    return tester_node
