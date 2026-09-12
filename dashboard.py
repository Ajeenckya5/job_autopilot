#!/usr/bin/python3
"""Local Job Autopilot web app.

Serves a browser UI on localhost: setup, find jobs, mailbox sync, and
the application funnel. Binds to localhost only.

  python3 dashboard.py
  python3 autopilot.py dashboard
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "docs" if (HERE / "docs" / "index.html").is_file() else HERE / "dashboard_static"
SECRETS_PATH = HERE / "config.yaml"
log = logging.getLogger("autopilot.dashboard")

FUNNEL_STATUSES = (
    "applied",
    "assessment",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
)
PROGRESS_STAGES = ("applied", "assessment", "interview", "offer")
STAGE_RANK = {name: i for i, name in enumerate(PROGRESS_STAGES)}
JOB_MAIL_STAGES = frozenset(
    {"applied", "assessment", "interview", "offer", "rejected", "withdrawn"}
)
MAIL_PREFIXES = ("ambiguous:", "no_status_change:")

TRACKS: dict[str, dict[str, Any]] = {}


def load_tracks() -> dict[str, dict[str, Any]]:
    """Discover ledgers: primary config.yaml output, plus ml/ and ops/ if present."""
    import os
    tracks: dict[str, dict[str, Any]] = {}
    secrets = _load_yaml(SECRETS_PATH)
    out_raw = ""
    if secrets:
        out_raw = str(secrets.get("output_dir") or "")
    out_dir = Path(os.path.expanduser(out_raw or str(HERE / "data"))).expanduser().resolve()
    label = "My search"
    if secrets:
        label = (
            secrets.get("excel_export_name")
            or (secrets.get("candidate") or {}).get("name")
            or "My search"
        )
    tracks["main"] = {
        "id": "main",
        "label": str(label),
        "db": out_dir / "autopilot.sqlite",
        "digest": out_dir / "digest.json",
        "config": SECRETS_PATH,
        "resume": "",
        "output_dir": out_dir,
    }
    for tid, nice, folder in (
        ("ml", "ML / AI", "ml"),
        ("ops", "Industrial / Ops", "ops"),
    ):
        db = HERE / folder / "autopilot.sqlite"
        if db.is_file() and db.stat().st_size > 1000:
            tracks[tid] = {
                "id": tid,
                "label": nice,
                "db": db,
                "digest": HERE / folder / "digest.json",
                "config": HERE / f"config_{tid}.yaml",
                "resume": str(HERE / f"resume_{tid}.txt"),
                "output_dir": HERE / folder,
            }
    return tracks

_sync_lock = threading.Lock()
_sync_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}
_scout_lock = threading.Lock()
_scout_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


def _setup_status() -> dict[str, Any]:
    import setup_wizard
    return setup_wizard.config_status(SECRETS_PATH)


def apply_setup(
    payload: dict[str, Any],
    *,
    resume_bytes: bytes | None = None,
    resume_name: str = "resume.pdf",
) -> dict[str, Any]:
    import setup_wizard
    path = setup_wizard.write_config(
        payload,
        SECRETS_PATH,
        resume_bytes=resume_bytes,
        resume_name=resume_name,
    )
    return {"ok": True, "path": str(path), "status": setup_wizard.config_status(path)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_label(week_key: str) -> str:
    try:
        year_s, week_s = week_key.split("-W")
        dt = datetime.strptime(f"{year_s} {int(week_s)} 1", "%G %V %u")
        return dt.strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return week_key


def canonical_mail_stage(classification: str | None) -> str:
    raw = (classification or "").strip().lower()
    for prefix in MAIL_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw or "ignored"


def _connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    if write:
        con = sqlite3.connect(str(path), timeout=60)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _selected_tracks(track: str) -> list[dict[str, Any]]:
    all_tracks = load_tracks()
    if track in all_tracks:
        return [all_tracks[track]]
    return list(all_tracks.values())


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _candidate_meta() -> dict[str, Any]:
    secrets = _load_yaml(SECRETS_PATH)
    candidate = secrets.get("candidate") or {}
    mail = secrets.get("mail_tracking") or {}
    gmail = secrets.get("gmail") or {}
    username = (
        mail.get("username")
        or gmail.get("address")
        or candidate.get("email")
        or ""
    )
    return {
        "name": candidate.get("name") or "",
        "email": username,
        "mail_enabled": bool(mail.get("enabled")),
        "mailboxes": mail.get("mailboxes") or ["INBOX"],
        "lookback_days": int(mail.get("lookback_days") or 90),
    }


def _read_digest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    counts = data.get("counts") or {}
    return {
        "generated_at": data.get("generated_at") or "",
        "run_tag": data.get("run_tag") or "",
        "new_this_run": int(counts.get("new_this_run") or 0),
        "blocked": int(counts.get("blocked") or 0),
        "low_match_filtered": int(counts.get("low_match_filtered") or 0),
        "mail_scanned": int(counts.get("mail_scanned") or 0),
        "mail_updated": int(counts.get("mail_updated") or 0),
        "shortlist": len(data.get("jobs") or []),
    }


def _query_track(meta: dict[str, Any]) -> dict[str, Any] | None:
    path: Path = meta["db"]
    if not path.is_file() or path.stat().st_size < 100:
        return None
    con = _connect(path)
    try:
        status_rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        by_status = {str(r["status"] or "new"): int(r["n"]) for r in status_rows}

        source_rows = con.execute(
            f"""SELECT source, COUNT(*) AS n FROM jobs
                WHERE status IN ({",".join("?" * len(FUNNEL_STATUSES))})
                GROUP BY source ORDER BY n DESC""",
            FUNNEL_STATUSES,
        ).fetchall()

        company_rows = con.execute(
            f"""SELECT company, COUNT(*) AS n,
                       SUM(CASE WHEN status='interview' THEN 1 ELSE 0 END) AS interviews,
                       SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                       SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) AS waiting
                FROM jobs
                WHERE status IN ({",".join("?" * len(FUNNEL_STATUSES))})
                  AND company IS NOT NULL AND trim(company) != ''
                GROUP BY company ORDER BY n DESC LIMIT 12""",
            FUNNEL_STATUSES,
        ).fetchall()

        score_rows = con.execute(
            f"""SELECT
                  CASE
                    WHEN match_score >= 90 THEN '90-100'
                    WHEN match_score >= 80 THEN '80-89'
                    WHEN match_score >= 70 THEN '70-79'
                    WHEN match_score >= 50 THEN '50-69'
                    ELSE '0-49'
                  END AS bucket,
                  COUNT(*) AS n
                FROM jobs
                WHERE status IN ({",".join("?" * len(FUNNEL_STATUSES))})
                GROUP BY bucket""",
            FUNNEL_STATUSES,
        ).fetchall()

        companies_n = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        distinct_companies = con.execute(
            f"""SELECT COUNT(DISTINCT company) FROM jobs
                WHERE status IN ({",".join("?" * len(FUNNEL_STATUSES))})""",
            FUNNEL_STATUSES,
        ).fetchone()[0]

        funnel_jobs = con.execute(
            f"""SELECT id, status, first_seen FROM jobs
                WHERE status IN ({",".join("?" * len(FUNNEL_STATUSES))})""",
            FUNNEL_STATUSES,
        ).fetchall()

        mail_rows = con.execute(
            """SELECT message_key, job_id, received_at, processed_at,
                      classification, confidence, match_score
               FROM mail_events"""
        ).fetchall()

        attention_jobs = con.execute(
            """SELECT id, title, company, location, source, url, status,
                      match_score, first_seen, skip_reason
               FROM jobs
               WHERE status IN ('interview', 'assessment', 'offer')
               ORDER BY CASE status
                            WHEN 'offer' THEN 0
                            WHEN 'interview' THEN 1
                            ELSE 2 END,
                        first_seen DESC"""
        ).fetchall()

        stale_applied = con.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE status='applied' AND first_seen < ?""",
            ((datetime.now(timezone.utc) - timedelta(days=21)).isoformat(),),
        ).fetchone()[0]
    finally:
        con.close()

    mail_by_job: dict[str, set[str]] = defaultdict(set)
    mail_seen: dict[str, dict[str, Any]] = {}
    last_mail_by_job: dict[str, tuple[str, str]] = {}
    last_processed = ""
    last_received = ""
    for row in mail_rows:
        key = row["message_key"]
        if key not in mail_seen:
            mail_seen[key] = {
                "stage": canonical_mail_stage(row["classification"]),
                "received_at": row["received_at"] or "",
                "processed_at": row["processed_at"] or "",
                "linked": bool(row["job_id"]),
                "confidence": int(row["confidence"] or 0),
            }
        if row["job_id"]:
            mail_by_job[row["job_id"]].add(
                canonical_mail_stage(row["classification"])
            )
            received = row["received_at"] or ""
            prev = last_mail_by_job.get(row["job_id"])
            if not prev or received > prev[0]:
                last_mail_by_job[row["job_id"]] = (
                    received, row["classification"] or ""
                )
        if (row["processed_at"] or "") > last_processed:
            last_processed = row["processed_at"] or ""
        if (row["received_at"] or "") > last_received:
            last_received = row["received_at"] or ""

    reached = {stage: 0 for stage in PROGRESS_STAGES}
    weekly_jobs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for job in funnel_jobs:
        status = job["status"] or "applied"
        stages = set(mail_by_job.get(job["id"], set()))
        if status in STAGE_RANK:
            stages.add(status)
        best = "applied"
        for stage in PROGRESS_STAGES:
            if stage in stages:
                best = stage
        for stage in PROGRESS_STAGES:
            if STAGE_RANK[best] >= STAGE_RANK[stage]:
                reached[stage] += 1
        dt = _parse_dt(job["first_seen"])
        if dt:
            weekly_jobs[_week_key(dt)][status] += 1

    weekly_mail: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    mail_stage_counts: dict[str, int] = defaultdict(int)
    linked_job_mail = 0
    unlinked_job_mail = 0
    for event in mail_seen.values():
        stage = event["stage"]
        mail_stage_counts[stage] += 1
        if stage in JOB_MAIL_STAGES:
            if event["linked"]:
                linked_job_mail += 1
            else:
                unlinked_job_mail += 1
        dt = _parse_dt(event["received_at"])
        if dt and stage in JOB_MAIL_STAGES:
            weekly_mail[_week_key(dt)][stage] += 1

    return {
        "id": meta["id"],
        "label": meta["label"],
        "db_path": str(path),
        "by_status": by_status,
        "reached": reached,
        "sources": [{"source": r["source"] or "unknown", "n": int(r["n"])} for r in source_rows],
        "companies": [
            {
                "company": r["company"],
                "n": int(r["n"]),
                "interviews": int(r["interviews"] or 0),
                "rejected": int(r["rejected"] or 0),
                "waiting": int(r["waiting"] or 0),
            }
            for r in company_rows
        ],
        "score_buckets": [
            {"bucket": r["bucket"], "n": int(r["n"])} for r in score_rows
        ],
        "watched_companies": int(companies_n or 0),
        "distinct_companies": int(distinct_companies or 0),
        "stale_applied_21d": int(stale_applied or 0),
        "weekly_jobs": weekly_jobs,
        "weekly_mail": weekly_mail,
        "mail_stage_counts": dict(mail_stage_counts),
        "mail_total": len(mail_seen),
        "linked_job_mail": linked_job_mail,
        "unlinked_job_mail": unlinked_job_mail,
        "last_mail_processed_at": last_processed,
        "last_mail_received_at": last_received,
        "attention_jobs": _enrich_jobs(
            [_job_row(r, meta["id"]) for r in attention_jobs], last_mail_by_job
        ),
        "digest": _read_digest(meta["digest"]),
        "funnel_jobs": len(funnel_jobs),
        "mail_events": mail_seen,
    }


def _enrich_jobs(
    jobs: list[dict[str, Any]], last_mail_by_job: dict[str, tuple[str, str]]
) -> list[dict[str, Any]]:
    for item in jobs:
        received, classification = last_mail_by_job.get(item["id"], ("", ""))
        item["last_mail_at"] = received
        stage = canonical_mail_stage(classification)
        item["last_mail_stage"] = "" if stage == "ignored" else stage
    return jobs


def _job_row(row: sqlite3.Row, track: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "track": track,
        "title": row["title"] or "",
        "company": row["company"] or "",
        "location": row["location"] or "",
        "source": row["source"] or "",
        "url": row["url"] or "",
        "status": row["status"] or "new",
        "match_score": int(row["match_score"] or 0),
        "first_seen": row["first_seen"] or "",
        "note": (row["skip_reason"] or "")[:280] if "skip_reason" in row.keys() else "",
        "last_mail_at": row["last_mail_at"] if "last_mail_at" in row.keys() else "",
        "last_mail_stage": row["last_mail_stage"] if "last_mail_stage" in row.keys() else "",
    }


def _merge_counts(items: list[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for item in items:
        for key, value in item.items():
            out[key] += int(value or 0)
    return dict(out)


def _merge_weekly(items: list[dict[str, dict[str, int]]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for weekly in items:
        for week, stages in weekly.items():
            for stage, n in stages.items():
                out[week][stage] += n
    return {week: dict(stages) for week, stages in out.items()}


def _series_from_weekly(weekly: dict[str, dict[str, int]], weeks: int = 16) -> list[dict]:
    if not weekly:
        return []
    keys = sorted(weekly)
    keys = keys[-weeks:]
    out = []
    for key in keys:
        row = {"week": key, "label": _week_label(key)}
        for stage in ("applied", "rejected", "interview", "assessment", "offer"):
            row[stage] = int(weekly.get(key, {}).get(stage, 0))
        row["total"] = sum(row[stage] for stage in ("applied", "rejected", "interview", "assessment", "offer"))
        out.append(row)
    return out


def _pct(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round(100.0 * num / den, 1)


def build_overview(track: str) -> dict[str, Any]:
    selected = _selected_tracks(track)
    loaded: list[dict[str, Any]] = []
    missing: list[str] = []
    for meta in selected:
        data = _query_track(meta)
        if data is None:
            missing.append(meta["id"])
        else:
            loaded.append(data)

    by_status = _merge_counts([t["by_status"] for t in loaded])
    reached = _merge_counts([t["reached"] for t in loaded])
    applied = int(by_status.get("applied", 0))
    assessment = int(by_status.get("assessment", 0))
    interview = int(by_status.get("interview", 0))
    offer = int(by_status.get("offer", 0))
    rejected = int(by_status.get("rejected", 0))
    withdrawn = int(by_status.get("withdrawn", 0))
    funnel = applied + assessment + interview + offer + rejected + withdrawn
    advanced = assessment + interview + offer
    responded = rejected + withdrawn + advanced
    waiting = applied

    mail_stage_counts: dict[str, int] = defaultdict(int)
    mail_keys: set[str] = set()
    linked_job_mail = 0
    unlinked_job_mail = 0
    last_processed = ""
    last_received = ""
    if track == "all" and len(loaded) > 1:
        weekly_mail_acc: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for t in loaded:
            for key, event in t["mail_events"].items():
                if key in mail_keys:
                    continue
                mail_keys.add(key)
                mail_stage_counts[event["stage"]] += 1
                if event["stage"] in JOB_MAIL_STAGES:
                    if event["linked"]:
                        linked_job_mail += 1
                    else:
                        unlinked_job_mail += 1
                if event["processed_at"] > last_processed:
                    last_processed = event["processed_at"]
                if event["received_at"] > last_received:
                    last_received = event["received_at"]
                dt = _parse_dt(event["received_at"])
                if dt and event["stage"] in JOB_MAIL_STAGES:
                    weekly_mail_acc[_week_key(dt)][event["stage"]] += 1
        weekly_mail = {week: dict(stages) for week, stages in weekly_mail_acc.items()}
        mail_total = len(mail_keys)
    else:
        for t in loaded:
            for stage, n in t["mail_stage_counts"].items():
                mail_stage_counts[stage] += n
            linked_job_mail += t["linked_job_mail"]
            unlinked_job_mail += t["unlinked_job_mail"]
            last_processed = max(last_processed, t["last_mail_processed_at"])
            last_received = max(last_received, t["last_mail_received_at"])
        weekly_mail = _merge_weekly([t["weekly_mail"] for t in loaded])
        mail_total = sum(t["mail_total"] for t in loaded)

    weekly_jobs = _merge_weekly([t["weekly_jobs"] for t in loaded])
    companies: dict[str, dict[str, int]] = {}
    for t in loaded:
        for row in t["companies"]:
            slot = companies.setdefault(
                row["company"],
                {"company": row["company"], "n": 0, "interviews": 0, "rejected": 0, "waiting": 0},
            )
            slot["n"] += row["n"]
            slot["interviews"] += row["interviews"]
            slot["rejected"] += row["rejected"]
            slot["waiting"] += row["waiting"]
    company_list = sorted(companies.values(), key=lambda r: r["n"], reverse=True)[:12]

    sources: dict[str, int] = defaultdict(int)
    for t in loaded:
        for row in t["sources"]:
            sources[row["source"]] += row["n"]

    score_buckets: dict[str, int] = defaultdict(int)
    for t in loaded:
        for row in t["score_buckets"]:
            score_buckets[row["bucket"]] += row["n"]

    attention = []
    for t in loaded:
        attention.extend(t["attention_jobs"])

    def _stamp(iso: str) -> float:
        dt = _parse_dt(iso)
        return dt.timestamp() if dt else 0.0

    attention.sort(
        key=lambda j: (
            {"offer": 0, "interview": 1, "assessment": 2}.get(j["status"], 9),
            -_stamp(j.get("first_seen") or ""),
        )
    )

    stale = sum(t["stale_applied_21d"] for t in loaded)
    watched = sum(t["watched_companies"] for t in loaded)
    distinct_companies = sum(t["distinct_companies"] for t in loaded)
    mail_job = sum(mail_stage_counts.get(s, 0) for s in JOB_MAIL_STAGES)
    receipts = int(mail_stage_counts.get("applied", 0))

    insights: list[dict[str, str]] = []
    if funnel:
        insights.append({
            "title": "Response rate",
            "value": f"{_pct(responded, funnel)}%",
            "detail": (
                f"{responded} of {funnel} matched applications have a later outcome "
                f"(rejection, assessment, interview, or offer). "
                f"{waiting} are still waiting on a reply."
            ),
        })
        insights.append({
            "title": "Interview conversion",
            "value": f"{_pct(reached.get('interview', interview), funnel)}%",
            "detail": (
                f"{reached.get('interview', interview)} applications reached interview "
                f"based on current status plus matched interview mail."
            ),
        })
    if receipts:
        insights.append({
            "title": "Mailbox → posting match",
            "value": f"{_pct(linked_job_mail, mail_job)}%",
            "detail": (
                f"{receipts} application-receipt emails sit in the mailbox. "
                f"{funnel} are matched to a known posting. "
                f"{unlinked_job_mail} job emails could not be linked confidently and were not used to change status."
            ),
        })
    if stale:
        insights.append({
            "title": "Aging applications",
            "value": str(stale),
            "detail": (
                f"{stale} applications are still marked waiting and were first logged "
                f"more than 21 days ago. Most of these will not receive a reply."
            ),
        })
    if assessment:
        insights.append({
            "title": "Assessments open",
            "value": str(assessment),
            "detail": "These are the only rows that currently need a take-home or online test completed.",
        })

    mail_week = _series_from_weekly(weekly_mail)
    this_week = mail_week[-1]["applied"] if mail_week else 0
    last_week = mail_week[-2]["applied"] if len(mail_week) > 1 else 0

    candidate = _candidate_meta()
    return {
        "generated_at": _now_iso(),
        "track": track,
        "candidate": candidate,
        "setup": _setup_status(),
        "sync": dict(_sync_state),
        "scout": dict(_scout_state),
        "missing_tracks": missing,
        "tracks": [
            {
                "id": t["id"],
                "label": t["label"],
                "funnel": t["funnel_jobs"],
                "applied": int(t["by_status"].get("applied", 0)),
                "interview": int(t["by_status"].get("interview", 0)),
                "assessment": int(t["by_status"].get("assessment", 0)),
                "rejected": int(t["by_status"].get("rejected", 0)),
                "offer": int(t["by_status"].get("offer", 0)),
                "new": int(t["by_status"].get("new", 0)),
                "digest": t["digest"],
            }
            for t in loaded
        ],
        "kpis": {
            "applied": funnel,
            "waiting": waiting,
            "interviews": interview,
            "assessments": assessment,
            "offers": offer,
            "rejected": rejected,
            "withdrawn": withdrawn,
            "responded": responded,
            "advanced": advanced,
            "response_rate": _pct(responded, funnel),
            "interview_rate": _pct(reached.get("interview", interview), funnel),
            "reached_interview": int(reached.get("interview", interview)),
            "reached_assessment": int(reached.get("assessment", assessment)),
            "new_queue": int(by_status.get("new", 0)),
            "ready_for_review": int(by_status.get("ready_for_review", 0)),
            "watched_companies": watched,
            "companies": distinct_companies,
            "stale_applied_21d": stale,
        },
        "funnel_current": {
            "applied": applied,
            "assessment": assessment,
            "interview": interview,
            "offer": offer,
            "rejected": rejected,
            "withdrawn": withdrawn,
            "waiting": waiting,
        },
        "funnel_reached": reached,
        "mailbox": {
            "enabled": candidate["mail_enabled"],
            "address": candidate["email"],
            "scanned": mail_total,
            "job_related": mail_job,
            "ignored": int(mail_stage_counts.get("ignored", 0)),
            "linked": linked_job_mail,
            "unlinked": unlinked_job_mail,
            "stages": dict(mail_stage_counts),
            "last_processed_at": last_processed,
            "last_received_at": last_received,
            "receipts_this_week": this_week,
            "receipts_last_week": last_week,
        },
        "weekly_jobs": _series_from_weekly(weekly_jobs),
        "weekly_mail": mail_week,
        "companies": company_list,
        "sources": [{"source": k, "n": v} for k, v in sorted(sources.items(), key=lambda kv: -kv[1])],
        "score_buckets": [
            {"bucket": b, "n": score_buckets.get(b, 0)}
            for b in ("90-100", "80-89", "70-79", "50-69", "0-49")
            if score_buckets.get(b)
        ],
        "attention": attention[:40],
        "insights": insights,
        "digests": [t["digest"] for t in loaded if t["digest"]],
    }


def _sanitize_search(q: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 .@+&/#-]", "", q)[:80].strip()


def list_pipeline(track: str, status: str, q: str, limit: int, offset: int) -> dict[str, Any]:
    statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
    if not statuses or statuses == ["all"]:
        statuses = list(FUNNEL_STATUSES)
    allowed = set(FUNNEL_STATUSES) | {"new", "ready_for_review", "skipped"}
    statuses = [s for s in statuses if s in allowed] or list(FUNNEL_STATUSES)
    q = _sanitize_search(q)
    limit = max(1, min(int(limit or 75), 250))
    offset = max(0, int(offset or 0))

    rows: list[dict[str, Any]] = []
    for meta in _selected_tracks(track):
        path: Path = meta["db"]
        if not path.is_file():
            continue
        con = _connect(path)
        try:
            placeholders = ",".join("?" * len(statuses))
            sql = f"""
                SELECT j.id, j.title, j.company, j.location, j.source, j.url,
                       j.status, j.match_score, j.first_seen, j.skip_reason
                FROM jobs j
                WHERE j.status IN ({placeholders})
            """
            args: list[Any] = list(statuses)
            if q:
                sql += " AND (j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ?)"
                like = f"%{q}%"
                args.extend([like, like, like])
            sql += " ORDER BY j.first_seen DESC"
            last_mail: dict[str, tuple[str, str]] = {}
            for mrow in con.execute(
                """SELECT job_id, received_at, classification
                   FROM mail_events
                   WHERE job_id IS NOT NULL AND job_id != ''
                   ORDER BY received_at DESC"""
            ):
                jid = mrow["job_id"]
                if jid not in last_mail:
                    last_mail[jid] = (mrow["received_at"] or "", mrow["classification"] or "")
            for row in con.execute(sql, args):
                item = _job_row(row, meta["id"])
                received, classification = last_mail.get(item["id"], ("", ""))
                item["last_mail_at"] = received
                stage = canonical_mail_stage(classification)
                item["last_mail_stage"] = "" if stage == "ignored" else stage
                rows.append(item)
        finally:
            con.close()

    rank = {"offer": 0, "interview": 1, "assessment": 2, "applied": 3, "rejected": 4, "withdrawn": 5}
    rows.sort(key=lambda r: (rank.get(r["status"], 9), r["first_seen"]), reverse=False)
    # Keep active pipeline first, then recency within status — re-sort active by date desc
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row["status"]].append(row)
    ordered: list[dict[str, Any]] = []
    for status_name in ("offer", "interview", "assessment", "applied", "rejected", "withdrawn", "ready_for_review", "new"):
        chunk = grouped.get(status_name, [])
        chunk.sort(key=lambda r: r["first_seen"], reverse=True)
        ordered.extend(chunk)
    for status_name, chunk in grouped.items():
        if status_name not in FUNNEL_STATUSES and status_name not in {"new", "ready_for_review"}:
            ordered.extend(chunk)

    total = len(ordered)
    page = ordered[offset: offset + limit]
    counts: dict[str, int] = defaultdict(int)
    for row in ordered:
        counts[row["status"]] += 1
    return {"total": total, "offset": offset, "limit": limit, "counts": dict(counts), "jobs": page}


def list_mail(track: str, stage: str, linked: str, limit: int, offset: int) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    want_stage = (stage or "job").strip().lower()
    want_linked = (linked or "all").strip().lower()

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for meta in _selected_tracks(track):
        path: Path = meta["db"]
        if not path.is_file():
            continue
        con = _connect(path)
        try:
            for row in con.execute(
                """SELECT message_key, job_id, received_at, processed_at, from_addr,
                          subject, classification, confidence, match_score, snippet
                   FROM mail_events
                   ORDER BY COALESCE(received_at, processed_at) DESC"""
            ):
                key = row["message_key"]
                if key in seen:
                    continue
                seen.add(key)
                cstage = canonical_mail_stage(row["classification"])
                is_linked = bool(row["job_id"])
                if want_stage == "job" and cstage not in JOB_MAIL_STAGES:
                    continue
                if want_stage not in {"", "all", "job"} and cstage != want_stage:
                    continue
                if want_linked == "linked" and not is_linked:
                    continue
                if want_linked == "unlinked" and is_linked:
                    continue
                rows.append({
                    "track": meta["id"],
                    "message_key": key,
                    "job_id": row["job_id"] or "",
                    "received_at": row["received_at"] or "",
                    "from_addr": row["from_addr"] or "",
                    "subject": row["subject"] or "",
                    "classification": row["classification"] or "",
                    "stage": cstage,
                    "confidence": int(row["confidence"] or 0),
                    "match_score": int(row["match_score"] or 0),
                    "linked": is_linked,
                    "ambiguous": str(row["classification"] or "").startswith("ambiguous:"),
                    "snippet": (row["snippet"] or "")[:240],
                })
        finally:
            con.close()

    rows.sort(key=lambda r: r["received_at"], reverse=True)
    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "events": rows[offset: offset + limit],
    }


def _mail_config():
    secrets = _load_yaml(SECRETS_PATH)
    from types import SimpleNamespace
    return SimpleNamespace(
        mail_tracking=secrets.get("mail_tracking") or {},
        gmail=secrets.get("gmail") or {},
        candidate=secrets.get("candidate") or {},
    )


def _run_mail_sync() -> None:
    import core
    import mail_tracker

    cfg = _mail_config()
    results = []
    try:
        for meta in load_tracks().values():
            path: Path = meta["db"]
            if not path.is_file():
                continue
            db = core.DB(path)
            try:
                result = mail_tracker.sync_mail_statuses(cfg, db)
                results.append({
                    "track": meta["id"],
                    "scanned": result.scanned,
                    "classified": result.classified,
                    "updated": result.updated,
                    "ambiguous": result.ambiguous,
                    "unlinked": result.unlinked,
                    "ignored": result.ignored,
                    "already_seen": result.already_seen,
                })
            finally:
                db.conn.close()
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["finished_at"] = _now_iso()
            _sync_state["error"] = None
            _sync_state["result"] = results
    except Exception as e:
        log.exception("dashboard mail-sync failed")
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["finished_at"] = _now_iso()
            _sync_state["error"] = str(e)[:400]
            _sync_state["result"] = results


def start_mail_sync() -> dict[str, Any]:
    with _sync_lock:
        if _sync_state["running"]:
            return dict(_sync_state)
        _sync_state["running"] = True
        _sync_state["started_at"] = _now_iso()
        _sync_state["finished_at"] = None
        _sync_state["error"] = None
        _sync_state["result"] = None
    threading.Thread(target=_run_mail_sync, name="mail-sync", daemon=True).start()
    return dict(_sync_state)


def _run_scout() -> None:
    import autopilot
    try:
        code = autopilot.cmd_scout(SimpleNamespace(config=str(SECRETS_PATH), verbose=False))
        with _scout_lock:
            _scout_state["running"] = False
            _scout_state["finished_at"] = _now_iso()
            _scout_state["error"] = None if code == 0 else f"scout exited {code}"
            _scout_state["result"] = {"ok": code == 0}
    except Exception as e:
        log.exception("dashboard scout failed")
        with _scout_lock:
            _scout_state["running"] = False
            _scout_state["finished_at"] = _now_iso()
            _scout_state["error"] = str(e)[:400]
            _scout_state["result"] = None


def start_scout() -> dict[str, Any]:
    status = _setup_status()
    if not status.get("configured"):
        raise ValueError("finish setup before finding jobs")
    with _scout_lock:
        if _scout_state["running"]:
            return dict(_scout_state)
        _scout_state["running"] = True
        _scout_state["started_at"] = _now_iso()
        _scout_state["finished_at"] = None
        _scout_state["error"] = None
        _scout_state["result"] = None
    threading.Thread(target=_run_scout, name="scout", daemon=True).start()
    return dict(_scout_state)


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    header = f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode("utf-8")
    msg = message_from_bytes(header + body, policy=HTTP)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        if filename:
            files[str(name)] = (filename, payload)
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[str(name)] = payload.decode(charset, errors="replace")
    return fields, files


def mark_job(track: str, job_id: str, status: str, note: str = "") -> dict[str, Any]:
    known = load_tracks()
    if track not in known:
        raise ValueError("unknown track")
    if status not in set(FUNNEL_STATUSES) | {"new", "ready_for_review"}:
        raise ValueError("invalid status")
    job_id = (job_id or "").strip()
    if not job_id:
        raise ValueError("missing job id")
    import core
    db = core.DB(known[track]["db"])
    try:
        row = db.conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("job not found")
        reason = note.strip() or f"Dashboard: marked {status}"
        db.mark(job_id, status, reason)
    finally:
        db.conn.close()
    return {"ok": True, "id": job_id, "track": track, "status": status}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "JobDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - " + fmt, self.address_string(), *args)

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        def q(name: str, default: str = "") -> str:
            vals = qs.get(name) or [default]
            return vals[0] if vals else default

        try:
            if path == "/api/health":
                self._json(200, {"ok": True, "generated_at": _now_iso()})
                return
            if path == "/api/overview":
                self._json(200, build_overview(q("track", "all") or "all"))
                return
            if path == "/api/pipeline":
                self._json(200, list_pipeline(
                    q("track", "all") or "all",
                    q("status", ""),
                    q("q", ""),
                    int(q("limit", "75") or 75),
                    int(q("offset", "0") or 0),
                ))
                return
            if path == "/api/mail":
                self._json(200, list_mail(
                    q("track", "all") or "all",
                    q("stage", "job"),
                    q("linked", "all"),
                    int(q("limit", "50") or 50),
                    int(q("offset", "0") or 0),
                ))
                return
            if path == "/api/setup-status":
                self._json(200, _setup_status())
                return
            if path == "/api/scout":
                self._json(200, dict(_scout_state))
                return
            if path == "/api/sync":
                self._json(200, dict(_sync_state))
                return
            self._serve_static(path)
        except Exception as e:
            log.exception("GET %s failed", path)
            if path.startswith("/api/"):
                self._json(500, {"error": str(e)[:300]})
            else:
                self.send_error(500, str(e)[:200])

    def _read_body(self, limit: int = 12_000_000) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return b""
        return self.rfile.read(length)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/setup":
                ctype = self.headers.get("Content-Type") or ""
                resume_bytes = None
                resume_name = "resume.pdf"
                if "multipart/form-data" in ctype:
                    fields, files = parse_multipart(ctype, self._read_body())
                    body = dict(fields)
                    if "resume_file" in files:
                        resume_name, resume_bytes = files["resume_file"]
                        if not resume_bytes:
                            resume_bytes = None
                else:
                    body = json.loads(self._read_body().decode("utf-8") or "{}")
                    if not isinstance(body, dict):
                        body = {}
                self._json(200, apply_setup(body, resume_bytes=resume_bytes, resume_name=resume_name))
                return
            if path == "/api/scout":
                self._json(200, start_scout())
                return
            if path == "/api/sync":
                self._json(200, start_mail_sync())
                return
            if path == "/api/jobs/mark":
                body = self._read_json()
                result = mark_job(
                    str(body.get("track") or ""),
                    str(body.get("id") or ""),
                    str(body.get("status") or ""),
                    str(body.get("note") or ""),
                )
                self._json(200, result)
                return
            self._json(404, {"error": "not found"})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            log.exception("POST %s failed", path)
            self._json(500, {"error": str(e)[:300]})

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        base = STATIC_DIR.resolve()
        target = (base / rel).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            self.send_error(404, "Not found")
            return
        data = target.read_bytes()
        if target.suffix == ".svg":
            ctype = "image/svg+xml"
        elif target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif target.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        else:
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def main(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> int:
    venv_py = Path.home() / ".venvs" / "job-autopilot" / "bin" / "python"
    if "yaml" not in sys.modules:
        try:
            import yaml  # noqa: F401
        except ImportError:
            if venv_py.is_file() and Path(sys.executable).resolve() != venv_py.resolve():
                os.execv(str(venv_py), [str(venv_py), *sys.argv])
            raise SystemExit(
                "PyYAML is required. Use the project venv: "
                "~/.venvs/job-autopilot/bin/python dashboard.py"
            )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not STATIC_DIR.is_dir():
        raise SystemExit(f"dashboard static files missing: {STATIC_DIR}")
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    url = f"http://{host}:{port}/"
    print(f"Job Autopilot: {url}")
    print("Open that address in a browser. Bound to localhost.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job search dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    raise SystemExit(main(args.host, args.port, open_browser=not args.no_open))
