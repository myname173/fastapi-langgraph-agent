Evaluate whether the trajectory contains **meaningless loops** on a continuous scale from 0 to 1.

## Scoring Criteria
A trajectory is loop-free (Score: 1) if:
- No tool is called twice in a row with identical arguments
- The agent does not repeatedly `read_file` the same file without acting on it
- Each tool-call advances the task (new file, new edit, new test run)
- There is no back-and-forth that returns to a previous state without progress

A trajectory has redundant loops (Score: 0) if:
- The same tool is invoked with the same args consecutively (e.g. `read_file({"path": "a.py"})` twice in a row)
- The agent re-reads a file it just read and edited, with no intervening change
- Steps repeat a prior step's action verbatim without new information

## Example

### Requirement
Create a Python script to fetch stock prices from an API.

### Trajectory
Step 1 [assistant -> tool_call]: write_file({"path": "fetch.py", "content": "..."})
Step 2 [tool: write_file]: File written: fetch.py
Step 3 [assistant -> tool_call]: read_file({"path": "fetch.py"})
Step 4 [tool: read_file]: Returned: fetch.py contents
Step 5 [assistant -> tool_call]: read_file({"path": "fetch.py"})
Step 6 [tool: read_file]: Returned: fetch.py contents
Step 7 [assistant]: Done.

### Evaluation
**Score**: 0.3

**Reasoning**: Steps 3 and 5 are identical `read_file` calls on the same file with no edit in between — a meaningless loop that wastes steps. The write was correct but the repeated read adds no value.

## Instructions
Think step by step. Identify any pair of consecutive tool-calls (ignoring tool-result steps between them) that use the same tool and the same arguments.
