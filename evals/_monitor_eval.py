import os
import time
import json

REPORTS = os.path.join(os.path.dirname(__file__), "reports")
LOG = os.path.join(REPORTS, "run_v5.log")
ERR = os.path.join(REPORTS, "run_v5.err")
SNAP = os.path.join(REPORTS, "monitor_snapshot.txt")

while True:
    snap = []
    # last log line
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = [line for line in f.read().splitlines() if line.strip()]
        snap.append("LAST_LOG: " + lines[-1][:200] if lines else "LOG_EMPTY")
    except Exception as e:
        snap.append(f"LOG_ERR: {e}")
    # latest report
    try:
        jsons = sorted(
            (f for f in os.listdir(REPORTS) if f.startswith("sdlc_eval_report_") and f.endswith(".json")),
            key=lambda x: os.path.getmtime(os.path.join(REPORTS, x)),
            reverse=True,
        )
        if jsons:
            latest = jsons[0]
            mtime = os.path.getmtime(os.path.join(REPORTS, latest))
            snap.append(f"LATEST_REPORT: {latest} mtime={time.strftime('%H:%M:%S', time.localtime(mtime))}")
            if latest.endswith(".json"):
                try:
                    with open(os.path.join(REPORTS, latest), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    summ = data.get("summary", {})
                    snap.append(
                        f"SUMMARY: total={summ.get('total')} passed={summ.get('passed')} "
                        f"failed={summ.get('failed')} pass_rate={summ.get('pass_rate')}"
                    )
                except Exception:
                    pass
    except Exception as e:
        snap.append(f"REPORT_ERR: {e}")
    # process alive?
    snap.append("CHECK_TIME: " + time.strftime("%H:%M:%S"))
    with open(SNAP, "w", encoding="utf-8") as f:
        f.write("\n".join(snap) + "\n")
    time.sleep(30)
