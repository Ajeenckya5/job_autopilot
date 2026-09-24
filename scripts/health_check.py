#!/usr/bin/env python3
"""Check the local dashboard and restart it when it is down."""

import time
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8787/api/health"
PYTHON = Path.home() / ".venvs" / "job-autopilot" / "bin" / "python"


def notify(message: str) -> None:
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "Job Autopilot"'],
        check=False,
    )
    log = Path.home() / "Library" / "Logs" / "job-autopilot-health.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    kept = []
    if log.is_file():
        for line in log.read_text().splitlines():
            try:
                stamp = datetime.strptime(line[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if stamp >= cutoff:
                kept.append(line)
    kept.append(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')} {message}")
    log.write_text("\n".join(kept) + "\n")


def healthy() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=4) as res:
            return res.status == 200 and b'"ok"' in res.read()
    except Exception:
        return False


def main() -> int:
    if healthy():
        return 0
    python = str(PYTHON if PYTHON.is_file() else "python3")
    subprocess.Popen(
        [python, str(ROOT / "dashboard.py"), "--no-open", "--port", "8787"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    notify("The Mac app was down. A restart was started.")
    for _ in range(8):
        time.sleep(1)
        if healthy():
            return 0
    notify("The Mac app could not restart.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
