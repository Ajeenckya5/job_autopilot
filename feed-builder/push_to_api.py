"""Send new and changed feed jobs to the jobs API, so search keeps finding this week's roles.

The feed in feeds/ is rebuilt every six hours, but the API's database only refreshed a handful of
boards on its own. This compares the rebuilt feed with the last committed one and posts what
changed, ten jobs per request to stay inside the free Worker and D1 per-request limits.

Skips quietly when INGEST_TOKEN is not set. Pass --all once to send the whole feed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("JOBS_API", "https://jobs-api.ajeenckyam8.workers.dev")
BATCH = 10


def jobs_from_payload(raw: bytes | str) -> dict[str, dict]:
    data = json.loads(raw)
    rows = data.get("jobs", []) if isinstance(data, dict) else data
    return {str(job.get("id")): job for job in rows if isinstance(job, dict) and job.get("id")}


def load_dir(path: Path) -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    for shard in sorted(path.glob("*.json")):
        if shard.name in {"manifest.json", "lexicon.json"}:
            continue
        jobs.update(jobs_from_payload(shard.read_bytes()))
    return jobs


def load_committed(folder: str = "feeds") -> dict[str, dict]:
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD", f"{folder}/"],
        capture_output=True, text=True, check=False,
    )
    jobs: dict[str, dict] = {}
    for name in listing.stdout.split():
        if not name.endswith(".json") or name.endswith("manifest.json") or name.endswith("lexicon.json"):
            continue
        shown = subprocess.run(["git", "show", f"HEAD:{name}"], capture_output=True, check=False)
        if shown.returncode == 0:
            jobs.update(jobs_from_payload(shown.stdout))
    return jobs


def changed_jobs(current: dict[str, dict], previous: dict[str, dict]) -> list[dict]:
    return [job for key, job in current.items() if previous.get(key) != job]


def post(batch: list[dict], token: str, attempts: int = 4) -> bool:
    body = json.dumps({"jobs": batch}).encode()
    for attempt in range(attempts):
        request = urllib.request.Request(
            f"{API}/v1/jobs",
            data=body,
            method="POST",
            headers={"content-type": "application/json", "x-ingest-token": token, "user-agent": "JobAutopilotFeed/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as err:
            if err.code in (401, 404):
                print(f"The API refused the ingest token ({err.code}). Check INGEST_TOKEN on both sides.")
                return False
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 ** attempt)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="send every job, not only changes")
    parser.add_argument("--feeds", default="feeds")
    args = parser.parse_args()
    token = os.environ.get("INGEST_TOKEN", "").strip()
    if not token:
        print("INGEST_TOKEN is not set; the API keeps its current jobs.")
        return 0
    current = load_dir(Path(args.feeds))
    todo = list(current.values()) if args.all else changed_jobs(current, load_committed(args.feeds))
    print(f"{len(todo)} of {len(current)} jobs to send.")
    sent = failed = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        if post(batch, token):
            sent += len(batch)
        else:
            failed += len(batch)
            if failed >= 200 and sent == 0:
                print("Stopping: nothing is getting through.")
                break
        time.sleep(0.05)
    print(f"Sent {sent}, failed {failed}.")
    return 1 if failed and not sent else 0


if __name__ == "__main__":
    sys.exit(main())
