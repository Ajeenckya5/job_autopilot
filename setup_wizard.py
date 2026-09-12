"""First-run setup for any job seeker.

Writes a local config.yaml from answers — resume, target roles, location,
how far back to look, how often to run, and the caller's own API keys.
Never prints or logs secret values.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import imap_presets

HERE = Path(__file__).resolve().parent

LLM_PRESETS = {
    "gemini": {
        "label": "Google Gemini (free tier)",
        "url": "https://aistudio.google.com/app/apikey",
        "model": "gemini-2.0-flash",
    },
    "groq": {
        "label": "Groq (free, fast)",
        "url": "https://console.groq.com/keys",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openai": {
        "label": "OpenAI",
        "url": "https://platform.openai.com/api-keys",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    "openrouter": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/keys",
        "model": "google/gemini-2.0-flash-exp:free",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "xai": {
        "label": "xAI Grok",
        "url": "https://console.x.ai/",
        "model": "grok-4",
        "base_url": "https://api.x.ai/v1",
    },
}

SCRAPER_PRESETS = {
    "jsearch": {
        "label": "JSearch (RapidAPI) — Google/LinkedIn/Indeed aggregator",
        "url": "https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch",
        "host": "jsearch.p.rapidapi.com",
    },
    "adzuna": {
        "label": "Adzuna Jobs API",
        "url": "https://developer.adzuna.com/",
    },
    "serpapi": {
        "label": "SerpAPI Google Jobs",
        "url": "https://serpapi.com/",
    },
    "none": {
        "label": "Built-in scrapers only (LinkedIn guest, Jobright, Glassdoor, YC)",
        "url": "",
    },
}


def config_status(config_path: Path | None = None) -> dict[str, Any]:
    path = Path(config_path or HERE / "config.yaml")
    missing: list[str] = []
    data: dict[str, Any] = {}
    if not path.is_file():
        return {
            "configured": False,
            "exists": False,
            "missing": ["config"],
            "path": str(path),
            "candidate": {},
            "roles": [],
            "location": "",
            "lookback_days": 7,
            "runs_per_day": 4,
            "max_years_required": 3,
            "llm_provider": "gemini",
            "scraper_type": "jsearch",
            "mail_enabled": False,
        }
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {
            "configured": False,
            "exists": True,
            "missing": ["config_parse"],
            "path": str(path),
            "candidate": {},
        }

    candidate = data.get("candidate") or {}
    resume = Path(os.path.expanduser(str(data.get("resume_pdf") or ""))).expanduser()
    roles = data.get("roles") or []
    providers = data.get("llm_providers") or []
    has_llm = any((p or {}).get("api_key") for p in providers) or bool(
        (data.get("xai") or {}).get("api_key")
    )
    scrapers = data.get("scraper_apis") or []
    has_scraper = any(
        (s or {}).get("api_key") or (s or {}).get("app_key") for s in scrapers
    )
    if not candidate.get("name"):
        missing.append("name")
    if not resume.is_file():
        missing.append("resume")
    if not roles:
        missing.append("roles")
    hours = int((data.get("behavior") or {}).get("max_age_hours") or 24)
    schedule = int(data.get("schedule_minutes") or 360)
    runs = max(1, round(1440 / schedule)) if schedule else 4
    loc = ""
    if roles:
        loc = str(roles[0].get("location") or "")
    llm_type = "gemini"
    for p in providers:
        if (p or {}).get("api_key"):
            llm_type = str((p or {}).get("type") or "gemini").lower()
            break
    scraper_type = "none"
    if scrapers:
        scraper_type = str((scrapers[0] or {}).get("type") or "none").lower()

    configured = not missing
    return {
        "configured": configured,
        "exists": True,
        "missing": missing,
        "path": str(path),
        "candidate": {
            "name": candidate.get("name") or "",
            "email": candidate.get("email") or "",
            "phone": candidate.get("phone") or "",
            "linkedin": candidate.get("linkedin") or "",
        },
        "resume_pdf": str(data.get("resume_pdf") or ""),
        "resume_ok": resume.is_file(),
        "roles": [r.get("keywords") for r in roles if r.get("keywords")],
        "location": loc,
        "lookback_days": max(1, round(hours / 24)),
        "runs_per_day": runs,
        "max_years_required": int((data.get("filters") or {}).get("max_years_required") or 3),
        "llm_provider": llm_type,
        "has_llm_key": has_llm,
        "scraper_type": scraper_type,
        "has_scraper_key": has_scraper,
        "mail_enabled": bool((data.get("mail_tracking") or {}).get("enabled")),
        "mail_provider": str((data.get("mail_tracking") or {}).get("provider") or "auto"),
        "imap_host": str((data.get("mail_tracking") or {}).get("imap_host") or ""),
        "presets": {
            "llm": {k: {"label": v["label"], "url": v["url"]} for k, v in LLM_PRESETS.items()},
            "scraper": {k: {"label": v["label"], "url": v["url"]} for k, v in SCRAPER_PRESETS.items()},
        },
    }


def _split_roles(raw: str | list) -> list[str]:
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw]
    else:
        parts = re.split(r"[\n,;]+", str(raw or ""))
    out = []
    seen = set()
    for part in parts:
        title = re.sub(r"\s+", " ", part).strip(" -")
        if len(title) < 2:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out[:40]


def build_config_dict(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    linkedin = str(payload.get("linkedin") or "").strip()
    resume = str(payload.get("resume_pdf") or "").strip()
    location = str(payload.get("location") or "United States").strip() or "United States"
    titles = _split_roles(payload.get("roles") or payload.get("titles") or "")
    lookback_days = max(1, min(int(payload.get("lookback_days") or 7), 60))
    runs_per_day = max(1, min(int(payload.get("runs_per_day") or 4), 24))
    max_years = max(0, min(int(payload.get("max_years_required") or 3), 20))
    llm_type = str(payload.get("llm_provider") or "gemini").strip().lower()
    llm_key = str(payload.get("llm_api_key") or "").strip()
    scraper_type = str(payload.get("scraper_type") or "none").strip().lower()
    scraper_key = str(payload.get("scraper_api_key") or "").strip()
    adzuna_app_id = str(payload.get("adzuna_app_id") or "").strip()
    mail_password = str(
        payload.get("mail_password") or payload.get("gmail_app_password") or ""
    ).strip()
    mail_provider = str(payload.get("mail_provider") or "auto").strip().lower()
    imap_host = str(payload.get("imap_host") or "").strip()
    output_dir = str(payload.get("output_dir") or str(HERE / "data")).strip()

    if not name:
        raise ValueError("name is required")
    if not titles:
        raise ValueError("add at least one job title you want")
    resume_path = Path(os.path.expanduser(resume)).expanduser()
    if not resume or not resume_path.is_file():
        raise ValueError("resume PDF not found — give a full path to your resume file")

    if llm_type not in LLM_PRESETS:
        llm_type = "gemini"
    preset = LLM_PRESETS[llm_type]
    providers = []
    xai = {"base_url": preset.get("base_url", "https://api.x.ai/v1"), "api_key": "", "model": preset["model"]}
    if llm_key:
        entry = {"type": llm_type, "api_key": llm_key, "model": preset["model"]}
        if preset.get("base_url"):
            entry["base_url"] = preset["base_url"]
        providers.append(entry)
        xai["api_key"] = llm_key
        xai["model"] = preset["model"]
        if preset.get("base_url"):
            xai["base_url"] = preset["base_url"]

    scraper_apis: list[dict[str, Any]] = []
    if scraper_type in {"jsearch", "serpapi"} and scraper_key:
        item = {"type": scraper_type, "api_key": scraper_key}
        if scraper_type == "jsearch":
            item["host"] = SCRAPER_PRESETS["jsearch"]["host"]
        scraper_apis.append(item)
    elif scraper_type == "adzuna" and scraper_key and adzuna_app_id:
        scraper_apis.append({
            "type": "adzuna",
            "app_id": adzuna_app_id,
            "app_key": scraper_key,
            "country": "us",
        })

    schedule_minutes = max(30, int(round(1440 / runs_per_day)))
    roles = [{"keywords": title, "location": location} for title in titles]
    mail_user = email
    mail_profile = imap_presets.resolve_mail_profile(
        mail_user,
        provider=mail_provider,
        imap_host=imap_host,
    )
    return {
        "candidate": {
            "name": name,
            "email": email,
            "phone": phone,
            "linkedin": linkedin,
        },
        "resume_pdf": str(resume_path),
        "roles": roles,
        "schedule_minutes": schedule_minutes,
        "max_per_run": 5000,
        "source_limits": {
            "linkedin_max_jobs_per_role": 1000,
            "indeed_max_pages_per_role": 50,
            "jobright_max_pages_per_role": 250,
            "watched_company_discovery_candidates": 8,
        },
        "throttle_seconds": 60,
        "output_dir": output_dir,
        "xai": xai,
        "llm_providers": providers,
        "scraper_apis": scraper_apis,
        "gmail": {
            "address": email,
            "app_password": mail_password,
            "send": False,
        },
        "mail_tracking": {
            "enabled": bool(mail_password),
            "provider": mail_profile["provider"],
            "imap_host": mail_profile["imap_host"],
            "imap_port": mail_profile["imap_port"],
            "username": mail_user,
            "app_password": mail_password,
            "mailboxes": mail_profile["mailboxes"],
            "lookback_days": max(30, lookback_days),
            "max_messages_per_mailbox": 500,
            "min_match_score": 70,
            "interval_minutes": schedule_minutes,
        },
        "anymail_finder": {"api_key": ""},
        "filters": {
            "block_title_keywords": [
                "senior", "sr.", "sr ", " sr", "staff", "principal",
                "lead ", "head of", "director", "manager", "vp ",
                "vice president", "architect",
            ],
            "max_years_required": max_years,
            "block_phd_required": True,
            "block_non_us": False,
            "block_no_sponsorship": False,
            "block_staffing_agencies": True,
        },
        "behavior": {
            "manual_apply_only": True,
            "min_resume_match_score": 55,
            "detail_enrich_min_score": 40,
            "tailor_resume": False,
            "require_verified_email": False,
            "auto_watch_companies": True,
            "max_age_hours": lookback_days * 24,
            "strict_freshness": True,
            "use_learned_patterns": False,
        },
        "auto_apply": {
            "enabled": False,
            "submit": False,
            "headless": True,
            "email_fallback": False,
        },
    }


def save_resume_bytes(data: bytes, filename: str = "resume.pdf") -> Path:
    dest_dir = HERE / "data" / "resumes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(filename or "resume.pdf").suffix.lower()
    if ext not in {".pdf", ".txt", ".doc", ".docx"}:
        ext = ".pdf"
    dest = dest_dir / f"resume{ext}"
    dest.write_bytes(data)
    return dest


def write_config(
    payload: dict[str, Any],
    config_path: Path | None = None,
    *,
    resume_bytes: bytes | None = None,
    resume_name: str = "resume.pdf",
) -> Path:
    path = Path(config_path or HERE / "config.yaml")
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = yaml.safe_load(path.read_text()) or {}
        except Exception:
            existing = {}

    if resume_bytes:
        payload["resume_pdf"] = str(save_resume_bytes(resume_bytes, resume_name))
    elif not str(payload.get("resume_pdf") or "").strip():
        payload["resume_pdf"] = existing.get("resume_pdf") or ""

    if not str(payload.get("llm_api_key") or "").strip():
        providers = existing.get("llm_providers") or []
        if providers and (providers[0] or {}).get("api_key"):
            payload["llm_api_key"] = providers[0]["api_key"]
            payload.setdefault("llm_provider", providers[0].get("type") or "gemini")
        elif (existing.get("xai") or {}).get("api_key"):
            payload["llm_api_key"] = existing["xai"]["api_key"]

    if not str(payload.get("scraper_api_key") or "").strip():
        scrapers = existing.get("scraper_apis") or []
        if scrapers:
            spec = scrapers[0] or {}
            payload["scraper_api_key"] = spec.get("api_key") or spec.get("app_key") or ""
            payload.setdefault("scraper_type", spec.get("type") or "none")
            if spec.get("app_id"):
                payload.setdefault("adzuna_app_id", spec.get("app_id"))

    form_mail_pw = str(
        payload.get("mail_password") or payload.get("gmail_app_password") or ""
    ).strip()
    if not form_mail_pw:
        payload["mail_password"] = (
            (existing.get("mail_tracking") or {}).get("app_password")
            or (existing.get("gmail") or {}).get("app_password")
            or ""
        )
        payload.setdefault(
            "mail_provider",
            (existing.get("mail_tracking") or {}).get("provider") or "auto",
        )
        payload.setdefault(
            "imap_host",
            (existing.get("mail_tracking") or {}).get("imap_host") or "",
        )

    if existing.get("output_dir"):
        payload.setdefault("output_dir", existing["output_dir"])
    payload.setdefault("output_dir", str(HERE / "data"))

    reuse_llm = not str(payload.get("llm_api_key") or "").strip() and bool(
        existing.get("llm_providers") or existing.get("xai")
    )
    reuse_scraper = (
        not str(payload.get("scraper_api_key") or "").strip()
        and str(payload.get("scraper_type") or "").lower() not in {"none"}
        and bool(existing.get("scraper_apis"))
    )

    cfg = build_config_dict(payload)
    if reuse_llm:
        if existing.get("llm_providers"):
            cfg["llm_providers"] = existing["llm_providers"]
        if existing.get("xai"):
            cfg["xai"] = existing["xai"]
    if reuse_scraper:
        cfg["scraper_apis"] = existing["scraper_apis"]
    if not form_mail_pw and existing.get("mail_tracking"):
        kept = dict(existing["mail_tracking"])
        kept["username"] = (cfg.get("candidate") or {}).get("email") or kept.get("username")
        if str(payload.get("mail_provider") or "auto").lower() not in {"auto", "detect", ""}:
            profile = imap_presets.resolve_mail_profile(
                kept.get("username") or "",
                provider=str(payload.get("mail_provider") or "auto"),
                imap_host=str(payload.get("imap_host") or ""),
            )
            kept["provider"] = profile["provider"]
            kept["imap_host"] = profile["imap_host"]
            kept["imap_port"] = profile["imap_port"]
            kept["mailboxes"] = profile["mailboxes"]
        cfg["mail_tracking"] = kept
        if existing.get("gmail"):
            cfg["gmail"] = existing["gmail"]
            cfg["gmail"]["address"] = kept.get("username") or cfg["gmail"].get("address")
    for key, val in existing.items():
        if key not in cfg:
            cfg[key] = val

    out_dir = Path(os.path.expanduser(str(cfg["output_dir"]))).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(exist_ok=True)
    header = (
        "# Generated by Job Autopilot setup on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.\n"
        "# This file contains API keys — do not commit it.\n\n"
    )
    path.write_text(header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _ask(prompt: str, default: str = "", *, required: bool = False, secret: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{hint}: ")
        val = (raw or "").strip()
        if secret and val:
            val = val.strip()
        if not val:
            val = default
        if val or not required:
            return val
        print("  required.")


def prompt_cli(config_path: Path | None = None) -> int:
    path = Path(config_path or HERE / "config.yaml")
    print()
    print("Job Autopilot — set up a search for any resume and any role.")
    print("Built-in boards still run. Paste your own LLM key for scoring,")
    print("and optionally a job-board API key for extra coverage.")
    print()
    if path.is_file():
        overwrite = _ask("config.yaml already exists. Overwrite? (y/N)", "n").lower()
        if overwrite not in {"y", "yes"}:
            print(f"Left {path} unchanged.")
            return 0

    name = _ask("Your name", required=True)
    email = _ask("Email", required=True)
    phone = _ask("Phone (optional)")
    linkedin = _ask("LinkedIn URL (optional)")
    resume = _ask("Full path to your resume PDF", str(Path.home() / "Downloads" / "resume.pdf"), required=True)
    titles = _ask("Jobs you want (comma-separated titles)", required=True)
    location = _ask("Location", "United States")
    lookback = _ask("How many days back to look for postings", "7")
    runs = _ask("How many times a day should it run", "4")
    years = _ask("Skip postings that require more than this many years", "3")

    print()
    print("Scoring API — pick one provider and paste YOUR key:")
    for key, meta in LLM_PRESETS.items():
        print(f"  {key:12} {meta['label']}  {meta['url']}")
    llm_type = _ask("LLM provider", "gemini").lower()
    llm_key = _ask("LLM API key (leave blank to score with keywords only)", secret=True)

    print()
    print("Scraper API — optional extra coverage on top of the built-in boards:")
    for key, meta in SCRAPER_PRESETS.items():
        extra = f"  {meta['url']}" if meta.get("url") else ""
        print(f"  {key:12} {meta['label']}{extra}")
    scraper_type = _ask("Scraper API", "jsearch").lower()
    scraper_key = ""
    adzuna_app_id = ""
    if scraper_type not in {"none", ""}:
        scraper_key = _ask("Scraper API key", secret=True)
        if scraper_type == "adzuna":
            adzuna_app_id = _ask("Adzuna app id")
    mail_pw = _ask("Mailbox password or app password (any email provider, optional)", secret=True)
    mail_provider = "auto"
    imap_host = ""
    if mail_pw:
        print("Mailbox provider: auto, gmail, outlook, yahoo, icloud, or other")
        mail_provider = _ask("Mailbox provider", "auto").lower()
        if mail_provider in {"other", "imap", "custom"}:
            imap_host = _ask("IMAP host", "imap.gmail.com")

    payload = {
        "name": name,
        "email": email,
        "phone": phone,
        "linkedin": linkedin,
        "resume_pdf": resume,
        "roles": titles,
        "location": location,
        "lookback_days": int(lookback or 7),
        "runs_per_day": int(runs or 4),
        "max_years_required": int(years or 3),
        "llm_provider": llm_type,
        "llm_api_key": llm_key,
        "scraper_type": scraper_type,
        "scraper_api_key": scraper_key,
        "adzuna_app_id": adzuna_app_id,
        "gmail_app_password": mail_pw,
        "mail_password": mail_pw,
        "mail_provider": mail_provider,
        "imap_host": imap_host,
        "output_dir": str(HERE / "data"),
    }
    try:
        write_config(payload, path)
    except ValueError as e:
        print(f"setup failed: {e}")
        return 1
    print()
    print(f"Wrote {path}")
    print("Next:")
    print("  python3 autopilot.py scout       # scrape + score + export")
    print("  python3 autopilot.py dashboard   # command center")
    print("  python3 autopilot.py daemon      # run on the schedule you chose")
    return 0
