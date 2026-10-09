@echo off
cd /d "c:\Users\13682\Desktop\fastapi-langgraph-agent-production-ready-template-master"
set TS=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%
set TS=%TS: =0%
set LOG=evals\reports\run_v29_%TS%.log
echo RUN_V29_LOG=%LOG% > evals\reports\run_v29_pid.txt
uv run python -m evals._run_eval_fixed >> "%LOG%" 2>&1
echo EVAL_EXIT=%ERRORLEVEL% >> "%LOG%"
