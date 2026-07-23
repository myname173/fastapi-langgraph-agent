Evaluate whether the trajectory stayed within a reasonable **step and token budget** on a continuous scale from 0 to 1.

## Scoring Criteria
A trajectory adheres to budget (Score: 1) if:
- The number of steps is proportionate to the task complexity (simple task ≤ 5 steps, complex task ≤ 8)
- No step is wasted on unrelated or redundant work
- The trajectory does not spiral into retries that a competent agent would avoid
- Total work is efficient — the task could not be done in dramatically fewer steps without sacrificing correctness

A trajectory exceeds budget (Score: 0) if:
- The step count is far higher than the task warrants (e.g. 12 steps for a one-line typo fix)
- Multiple retry rounds appear without new information
- The agent explores many files unrelated to the requirement
- Steps are dominated by no-op or repeated actions

## Example

### Requirement
Update docker-compose.yml to add a redis service on port 6379.

### Trajectory
Step 1 [assistant -> tool_call]: read_file({"path": "docker-compose.yml"})
Step 2 [tool: read_file]: Returned: docker-compose.yml contents
Step 3 [assistant -> tool_call]: edit_file({"path": "docker-compose.yml", "old_str": "services:", "new_str": "services:\n  redis:"})
Step 4 [tool: edit_file]: Edited: docker-compose.yml
Step 5 [assistant]: Added redis service to docker-compose.yml.

### Evaluation
**Score**: 1.0

**Reasoning**: 5 steps for a single-service addition is proportionate: one read, one edit, one summary. No wasted steps, no retries, no unrelated exploration.

## Instructions
Think step by step. Judge step count against task complexity and penalize wasted or redundant work.
