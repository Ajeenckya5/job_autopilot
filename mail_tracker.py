"""Read-only mailbox scanner that reconciles job emails back into SQLite.

The tracker is intentionally conservative. It only updates a job when both the
email intent and the job match are clear; ambiguous messages are logged for
audit without changing job status.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import make_header, decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import core

log = logging.getLogger("autopilot.mail_tracker")


@dataclass
class MailSyncResult:
    scanned: int = 0
    classified: int = 0
    updated: int = 0
    ambiguous: int = 0
    unlinked: int = 0
    ignored: int = 0
    already_seen: int = 0


_GENERIC_COMPANY_WORDS = {
    "the", "inc", "llc", "ltd", "corp", "corporation", "company", "co",
    "labs", "lab", "technologies", "technology", "systems", "group",
    "holdings", "global", "ai", "software", "solutions",
}

_GENERIC_TITLE_WORDS = {
    "engineer", "engineering", "scientist", "science", "analyst", "developer",
    "software", "role", "job", "position", "new", "grad", "early", "career",
    "intern", "internship", "associate", "level", "remote", "united", "states",
}

_STATUS_PRIORITY = {
    "new": 0,
    "ready_for_review": 1,
    "applied": 2,
    "assessment": 3,
    "interview": 4,
    "rejected": 5,
    "withdrawn": 5,
    "offer": 6,
    "skipped": 99,
    "error": 99,
}

_STATUS_PATTERNS: list[tuple[str, int, tuple[str, ...]]] = [
    (
        "offer",
        98,
        (
            r"\boffer of employment\b",
            r"\bpleased to offer\b",
            r"\bwould like to offer you\b",
            r"\bextend(?:ing)? an offer\b",
            r"\bcongratulations\b.{0,120}\boffer\b",
        ),
    ),
    (
        "rejected",
        92,
        (
            r"\bwill not be moving forward\b",
            r"\bnot moving forward\b",
            r"\bnot be moving forward\b",
            r"\bnot proceed(?:ing)?\b",
            r"\bnot selected\b",
            r"\bnot chosen\b",
            r"\bdecided to (?:move forward|proceed) with other candidates\b",
            r"\bpursue other candidates\b",
            r"\bmove forward with other candidates\b",
            r"\bunable to offer\b",
            r"\bnot a fit\b",
            r"\bnot the right fit\b",
            r"\bapplication\b.{0,80}\b(?:declined|unsuccessful)\b",
            r"\bcandidacy\b.{0,80}\b(?:declined|unsuccessful)\b",
            r"\bposition has been filled\b",
            r"\bunfortunately\b.{0,180}\b(?:not|unable|other candidates|moving forward)\b",
        ),
    ),
    (
        "withdrawn",
        86,
        (
            r"\bapplication withdrawn\b",
            r"\bwithdrawn your application\b",
            r"\byou withdrew\b",
        ),
    ),
    (
        "assessment",
        84,
        (
            r"\bonline\s+assessment\b",
            r"\btechnical\s+assessment\b",
            r"\bcoding\s+assessment\b",
            r"\bcoding\s+challenge\b",
            r"\btechnical\s+challenge\b",
            r"\bhackerrank\b",
            r"\bcodesignal\b",
            r"\btake[- ]home\b",
            # Candidate must be asked to take/complete the assessment
            r"\b(?:complete|take|finish|start)\b.{0,60}\b(?:the\s+)?assessment\b",
            r"\b(?:invite[d]?|inviting)\b.{0,80}\bassessment\b",
            r"\bassessment\b.{0,60}\b(?:link|portal|url|deadline)\b",
        ),
    ),
    (
        "interview",
        84,
        (
            r"\bphone\s+(?:screen|interview)\b",
            r"\brecruiter\s+(?:screen|call|interview)\b",
            # Explicit invitation to you for an interview/screen
            r"\b(?:invite[d]?|inviting)\b.{0,100}\b(?:interview|screen|speak|connect|call)\b",
            r"\bschedule\b.{0,80}\b(?:call|chat|conversation|interview)\b",
            r"\bavailability\b.{0,100}\b(?:call|chat|interview|meet|speak)\b",
            r"\bcalendly\b",
            # You are being advanced/selected for an interview
            r"\b(?:advance[d]?|advancing|selected|moving\s+you\s+forward)\b.{0,80}\b(?:interview|next\s+round|screen)\b",
            # "your next step is/will be a call/interview"
            r"\byour\s+next\s+step\b.{0,80}\b(?:call|interview|screen)\b",
        ),
    ),
    (
        "applied",
        74,
        (
            r"\bthank you for applying\b",
            r"\bthanks for applying\b",
            r"\bwe received your application\b",
            r"\byour application (?:has been |was )?received\b",
            r"\bapplication submitted\b",
            r"\bsuccessfully submitted\b",
            r"\bthank you for your interest\b",
            r"\bthanks for your interest\b",
        ),
    ),
]


def sync_mail_statuses(
    cfg: core.Config,
    db: core.DB,
    *,
    dry_run: bool = False,
    force: bool = False,
    days: int | None = None,
    limit: int | None = None,
) -> MailSyncResult:
    """Scan configured mailboxes and update job statuses from application mail."""
    settings = dict(cfg.mail_tracking or {})
    username = (
        settings.get("username")
        or cfg.gmail.get("address")
        or cfg.candidate.get("email")
        or ""
    )
    password = settings.get("app_password") or cfg.gmail.get("app_password") or ""
    host = settings.get("imap_host") or "imap.gmail.com"
    port = int(settings.get("imap_port") or 993)
    mailboxes = settings.get("mailboxes") or ["INBOX"]
    lookback_days = int(days or settings.get("lookback_days") or 90)
    max_messages = int(limit or settings.get("max_messages_per_mailbox") or 500)
    min_match_score = int(settings.get("min_match_score") or 70)

    if not username or not password:
        raise RuntimeError(
            "mail tracking needs IMAP credentials. Set mail_tracking.username "
            "and mail_tracking.app_password, or reuse gmail.address/gmail.app_password."
        )

    result = MailSyncResult()
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")

    log.info("mail-sync: connecting to %s:%s as %s", host, port, username)
    with imaplib.IMAP4_SSL(host, port) as imap:
        imap.login(username, password)
        for mailbox in mailboxes:
            mailbox_name = str(mailbox)
            try:
                typ, _ = imap.select(f'"{mailbox_name}"', readonly=True)
                if typ != "OK":
                    log.warning("mail-sync: mailbox not selectable: %s", mailbox_name)
                    continue
            except Exception as e:
                log.warning("mail-sync: mailbox %s skipped: %s", mailbox_name, e)
                continue

            typ, data = imap.uid("search", None, "SINCE", since)
            if typ != "OK" or not data:
                continue
            uids = data[0].split()
            if max_messages > 0:
                uids = uids[-max_messages:]

            for uid in uids:
                _process_uid(
                    imap, mailbox_name, uid, cfg, db, result,
                    dry_run=dry_run,
                    force=force,
                    min_match_score=min_match_score,
                )
    return result


def _process_uid(
    imap: imaplib.IMAP4_SSL,
    mailbox: str,
    uid: bytes,
    cfg: core.Config,
    db: core.DB,
    result: MailSyncResult,
    *,
    dry_run: bool,
    force: bool,
    min_match_score: int,
) -> None:
    typ, data = imap.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data:
        return
    raw = next((part[1] for part in data if isinstance(part, tuple)), None)
    if not raw:
        return

    msg = email.message_from_bytes(raw, policy=policy.default)
    message_key = _message_key(msg, mailbox, uid)
    if not force and db.mail_event_seen(message_key):
        result.already_seen += 1
        return

    result.scanned += 1
    subject = _decode_header_value(msg.get("Subject", ""))
    from_addr = _format_addresses(msg.get("From", ""))
    received_at = _message_date(msg)
    body = _message_text(msg)
    combined = _squash(f"{subject}\n{from_addr}\n{body}")[:12000]

    classification, confidence, evidence = _classify(combined)
    if not classification:
        result.ignored += 1
        if not dry_run:
            db.log_mail_event({
                "message_key": message_key,
                "received_at": received_at,
                "from_addr": from_addr,
                "subject": subject,
                "classification": "ignored",
                "confidence": 0,
                "match_score": 0,
                "snippet": combined[:300],
            })
        return

    result.classified += 1
    match = _match_job(db, subject, body, from_addr, min_match_score)
    if not match:
        result.unlinked += 1
        if not dry_run:
            db.log_mail_event({
                "message_key": message_key,
                "received_at": received_at,
                "from_addr": from_addr,
                "subject": subject,
                "classification": classification,
                "confidence": confidence,
                "match_score": 0,
                "snippet": _event_snippet(evidence, combined),
            })
        return

    job, match_score, ambiguous = match
    if ambiguous:
        result.ambiguous += 1
        if not dry_run:
            db.log_mail_event({
                "message_key": message_key,
                "job_id": job["id"],
                "received_at": received_at,
                "from_addr": from_addr,
                "subject": subject,
                "classification": f"ambiguous:{classification}",
                "confidence": confidence,
                "match_score": match_score,
                "snippet": _event_snippet(evidence, combined),
            })
        return

    new_status = classification
    old_status = job["status"] or "new"
    if not _should_update(old_status, new_status):
        result.ignored += 1
        if not dry_run:
            db.log_mail_event({
                "message_key": message_key,
                "job_id": job["id"],
                "received_at": received_at,
                "from_addr": from_addr,
                "subject": subject,
                "classification": f"no_status_change:{classification}",
                "confidence": confidence,
                "match_score": match_score,
                "snippet": _event_snippet(evidence, combined),
            })
        return

    note = _status_note(new_status, subject, from_addr, evidence)
    if dry_run:
        log.info(
            "mail-sync dry-run: %s -> %s (%s @ %s, match=%d, confidence=%d)",
            old_status, new_status, job["title"], job["company"], match_score, confidence,
        )
    else:
        db.mark(job["id"], new_status, note)
        db.log_mail_event({
            "message_key": message_key,
            "job_id": job["id"],
            "received_at": received_at,
            "from_addr": from_addr,
            "subject": subject,
            "classification": classification,
            "confidence": confidence,
            "match_score": match_score,
            "snippet": _event_snippet(evidence, combined),
        })
    result.updated += 1


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:
        return value or ""


def _format_addresses(value: str) -> str:
    pairs = getaddresses([value or ""])
    if not pairs:
        return value or ""
    return ", ".join(
        f"{name} <{addr}>" if name else addr
        for name, addr in pairs
        if addr
    )


def _message_date(msg: Message) -> str:
    raw = msg.get("Date", "")
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _message_key(msg: Message, mailbox: str, uid: bytes) -> str:
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    if message_id:
        return message_id.strip("<>")
    return f"{mailbox}:{uid.decode(errors='ignore')}"


def _message_text(msg: Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "").lower()
            if disp == "attachment":
                continue
            if ctype in {"text/plain", "text/html"}:
                parts.append(_payload_to_text(part, html=(ctype == "text/html")))
    else:
        parts.append(_payload_to_text(msg, html=(msg.get_content_type() == "text/html")))
    return _squash("\n".join(p for p in parts if p))


def _payload_to_text(part: Message, *, html: bool) -> str:
    try:
        payload = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        if isinstance(payload, bytes):
            payload = payload.decode(charset, errors="replace")
    if not isinstance(payload, str):
        payload = str(payload)
    if html:
        payload = BeautifulSoup(payload, "lxml").get_text(" ", strip=True)
    return unescape(payload)


def _classify(text: str) -> tuple[str, int, str]:
    low = text.lower()
    for status, confidence, patterns in _STATUS_PATTERNS:
        for pattern in patterns:
            m = re.search(pattern, low, re.I | re.S)
            if m:
                return status, confidence, _squash(m.group(0))[:220]
    return "", 0, ""


def _match_job(
    db: core.DB,
    subject: str,
    body: str,
    from_addr: str,
    min_match_score: int,
) -> tuple[dict, int, bool] | None:
    rows = db.conn.execute(
        """
        SELECT id, title, company, url, status, first_seen, match_score
        FROM jobs
        WHERE status NOT IN ('skipped', 'error')
        ORDER BY
            CASE status
                WHEN 'applied' THEN 0
                WHEN 'assessment' THEN 1
                WHEN 'interview' THEN 2
                WHEN 'ready_for_review' THEN 3
                WHEN 'new' THEN 4
                ELSE 5
            END,
            first_seen DESC
        """
    ).fetchall()
    text = _normalize(f"{subject} {body}")[:14000]
    from_text = _normalize(from_addr)

    scored: list[tuple[int, dict]] = []
    for row in rows:
        job = dict(row)
        score = _job_match_score(job, text, from_text)
        if score:
            scored.append((score, job))
    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1].get("match_score") or 0), reverse=True)
    best_score, best_job = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0

    if best_score < min_match_score:
        return None
    ambiguous = len(scored) > 1 and second_score >= best_score - 12
    return best_job, best_score, ambiguous


def _job_match_score(job: dict, text: str, from_text: str) -> int:
    score = 0
    company = _company_base(job.get("company") or "")
    company_tokens = _tokens(company, _GENERIC_COMPANY_WORDS)
    title = _normalize(job.get("title") or "")
    title_tokens = _tokens(title, _GENERIC_TITLE_WORDS)

    if company and len(company) >= 4 and company in text:
        score += 58
    elif company_tokens:
        token_hits = sum(1 for token in company_tokens if token in text)
        score += min(48, token_hits * 18)

    if company_tokens and any(token in from_text for token in company_tokens):
        score += 30

    domain_hint = _domain_hint(job)
    if domain_hint and domain_hint in from_text:
        score += 28

    if title and len(title) >= 8 and title in text:
        score += 45
    if title_tokens:
        hits = sum(1 for token in title_tokens if token in text)
        score += min(36, hits * 8)

    status = job.get("status") or "new"
    if status == "applied":
        score += 12
    elif status in {"assessment", "interview"}:
        score += 8
    elif status == "ready_for_review":
        score += 4

    if score >= 55 and title_tokens and not any(token in text for token in title_tokens):
        same_company_active = _company_base(job.get("company") or "") in text
        if same_company_active:
            score += 15
    return score


def _company_base(company: str) -> str:
    company = re.sub(r"\([^)]*\)", " ", company or "")
    company = re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co)\b\.?", " ", company, flags=re.I)
    return _normalize(company)


def _domain_hint(job: dict) -> str:
    host = urlparse(job.get("url") or "").netloc.lower().replace("www.", "")
    board_hosts = (
        "linkedin.com", "indeed.com", "jobright.ai", "greenhouse.io",
        "lever.co", "workable.com", "ashbyhq.com", "bamboohr.com",
    )
    if host and not any(host == h or host.endswith("." + h) for h in board_hosts):
        return host.split(".")[0]
    company = _company_base(job.get("company") or "")
    tokens = _tokens(company, _GENERIC_COMPANY_WORDS)
    return tokens[0] if tokens else ""


def _tokens(text: str, stopwords: set[str]) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", _normalize(text))
        if len(token) >= 3 and token not in stopwords
    ]


def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _should_update(old_status: str, new_status: str) -> bool:
    old = old_status or "new"
    if old in {"skipped", "error"}:
        return False
    if old == new_status:
        return False
    if new_status == "offer":
        return True
    if old == "offer":
        return False
    if new_status == "rejected":
        return old not in {"rejected", "withdrawn"}
    if new_status == "withdrawn":
        return old not in {"rejected", "withdrawn"}
    if new_status == "interview":
        return old in {"new", "ready_for_review", "applied", "assessment"}
    if new_status == "assessment":
        return old in {"new", "ready_for_review", "applied"}
    if new_status == "applied":
        return old in {"new", "ready_for_review"}
    return _STATUS_PRIORITY.get(new_status, 0) > _STATUS_PRIORITY.get(old, 0)


def _status_note(status: str, subject: str, from_addr: str, evidence: str) -> str:
    subject = _squash(subject)[:180]
    from_addr = _squash(from_addr)[:120]
    evidence = _squash(evidence)[:180]
    return f"Mail tracker: {status}; from={from_addr}; subject={subject}; evidence={evidence}"


def _event_snippet(evidence: str, text: str) -> str:
    evidence = _squash(evidence)
    if evidence:
        return evidence
    return _squash(text)[:300]
