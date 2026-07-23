Evaluate the **accuracy of tool arguments** in the trajectory on a continuous scale from 0 to 1.

## Scoring Criteria
A trajectory has accurate tool args (Score: 1) if:
- File paths in `read_file`/`write_file`/`edit_file` match the files mentioned in the requirement
- `edit_file` args include a non-empty `old_str` that plausibly exists and a `new_str` that addresses the requirement
- `run_tests` targets the file that was just modified
- MCP tool args (e.g. `mcp_query_tasks`) use reasonable filters (`status`, `limit`) rather than empty or absurd values
- No argument is left empty or set to a placeholder when a real value is expected

A trajectory has poor tool args (Score: 0) if:
- Paths do not correspond to the requirement (e.g. editing `foo.py` when the task is about `auth.py`)
- `edit_file` has an empty `old_str` (a replace with no search target)
- Args are missing, null, or obviously wrong (e.g. `limit: -1`)
- The same generic args are reused across unrelated calls

## Example

### Requirement
Add a /health endpoint to the FastAPI app that returns status ok.

### Trajectory
Step 1 [assistant -> tool_call]: read_file({"path": "main.py"})
Step 2 [tool: read_file]: Returned: main.py contents
Step 3 [assistant -> tool_call]: edit_file({"path": "main.py", "old_str": "app = FastAPI()", "new_str": "app = FastAPI()\n@app.get('/health')"})
Step 4 [tool: edit_file]: Edited: main.py

### Evaluation
**Score**: 0.9

**Reasoning**: Paths are correct (main.py), the edit anchors on a plausible existing string, and the new_str adds the health route. Minor deduction because the response body is not shown, but args are accurate.

## Instructions
Think step by step. Focus only on argument correctness, not on whether the tool was the right choice (that is a separate metric).
