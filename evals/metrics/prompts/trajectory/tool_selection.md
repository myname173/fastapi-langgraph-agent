Evaluate whether the agent selected the **right tools** for the task on a continuous scale from 0 to 1.

## Scoring Criteria
A trajectory has good tool selection (Score: 1) if it:
- Uses tools that are appropriate for the requirement (e.g. `read_file` before editing, `run_tests` after writing code)
- Does NOT call tools that are irrelevant to the task (e.g. `security_scan` for a typo fix)
- Calls `read_file` to inspect existing code before modifying it when the task touches an existing file
- Prefers direct file tools (`write_file`/`edit_file`) over roundabout approaches
- Does NOT omit a necessary tool (e.g. writing code but never running tests when the task implies verification)

A trajectory has poor tool selection (Score: 0) if it:
- Skips inspection and blindly edits a file it has not read
- Uses a heavy tool (e.g. `security_scan`, `generate_and_run_script`) where a simple `edit_file` would do
- Calls tools unrelated to the requirement
- Omits `run_tests` when the task is a bug fix or refactor that risks regressions

## Example

### Requirement
Fix the null pointer bug in the auth module when user_id is missing.

### Trajectory
Step 1 [assistant -> tool_call]: edit_file({"path": "auth.py", "old_str": "return user.id", "new_str": "return user.id if user else None"})
Step 2 [tool: edit_file]: Edited: auth.py
Step 3 [assistant]: Done.

### Evaluation
**Score**: 0.4

**Reasoning**: The agent edited auth.py without first reading it to locate the bug, and did not run tests to verify the fix — both omissions are high-risk for a bug fix. Tool selection is incomplete.

## Instructions
Think step by step. Compare the tools actually used against what a careful engineer would use for the requirement.
