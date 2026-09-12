#!/usr/bin/env python3
"""Job Autopilot - local agent that finds fresh jobs across LinkedIn, Indeed,
Jobright, and watched company careers pages, watches every company it's seen, and
exports scored matches to Excel for manual applications.

Usage:
  ./autopilot.py init                      # interactive setup: resume, roles, location, APIs
  ./autopilot.py init --template           # copy config.example.yaml instead
  ./autopilot.py scout                     # search + rank only, no applying
  ./autopilot.py run                       # one pass: search + Excel export
  ./autopilot.py daemon                    # loop forever on schedule
  ./autopilot.py status                    # DB stats
  ./autopilot.py watch <domain> [name]     # manually add a company
  ./autopilot.py unwatch <domain>          # remove a company
  ./autopilot.py list-watched              # show watchlist
  ./autopilot.py dry <url>                 # one-shot: score a single URL
  ./autopilot.py mail-sync                 # reconcile application emails to Excel
  ./autopilot.py dashboard                 # local web app: setup, find jobs, mailbox, funnel
  ./autopilot.py login                     # log in to LinkedIn/Indeed/Jobright (run once)
  ./autopilot.py login linkedin            # log in to LinkedIn only
  ./autopilot.py login indeed jobright     # log in to Indeed + Jobright only
  ./autopilot.py review                    # interactive terminal: rate jobs, trains scorer
  ./autopilot.py feedback <id> staffing    # flag a company as staffing agency
  ./autopilot.py feedback <id> ok          # confirm a company as direct employer
  ./autopilot.py feedback <id> relevant    # mark a job as relevant (boosts similar)
  ./autopilot.py feedback <id> irrelevant  # mark a job as irrelevant (penalizes similar)
  ./autopilot.py learn                     # synthesize new filter+score patterns from feedback
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import time
from pathlib import Path

_REPOST_RE = re.compile(
    r'\bre[-\s]?post(?:ed|ing|s)?\b'
    r'|\bpreviously\s+(?:posted|listed|advertised)\b'
    r'|\bposting\s+again\b'
    r'|\bopen\s+again\b'
    r'|\bstill\s+(?:hiring|accepting|open)\b.{0,60}?\b(?:posting|position|role)\b',
    re.IGNORECASE,
)

import schedule

import core
import mail_tracker
import sources

log = logging.getLogger("autopilot")

# ── Terminal colours ─────────────────────────────────────────────────────────
_IS_TTY = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text

def _cyan(t: str)   -> str: return _c("96", t)
def _green(t: str)  -> str: return _c("92", t)
def _yellow(t: str) -> str: return _c("93", t)
def _red(t: str)    -> str: return _c("91", t)
def _bold(t: str)   -> str: return _c("1",  t)

_STATUS_COLOR = {
    "applied":         _green,
    "ready_for_review": _cyan,
    "skipped":         _yellow,
    "error":           _red,
}
HERE = Path(__file__).resolve().parent

# Domains that are job boards / ATS platforms — not real employers to watch.
_BOARD_DOMAINS = frozenset({
    "linkedin.com", "indeed.com", "jobright.ai", "greenhouse.io",
    "lever.co", "workable.com", "bamboohr.com", "glassdoor.com",
    "ziprecruiter.com", "monster.com", "simplyhired.com", "dice.com",
    "builtinnyc.com", "builtin.com", "angel.co",
})


def cmd_init(args) -> int:
    import setup_wizard
    if getattr(args, "template", False):
        cfg_path = Path(args.config)
        if cfg_path.exists() and not getattr(args, "force", False):
            print(f"{cfg_path} already exists.")
            return 1
        shutil.copy(HERE / "config.example.yaml", cfg_path)
        print(f"Wrote {cfg_path} from the example template.")
        print("Fill in resume_pdf, roles, location, and your API keys, then run:")
        print("  python3 autopilot.py scout")
        return 0
    return setup_wizard.prompt_cli(Path(args.config))


def _bootstrap_state(args):
    cfg = core.Config.load(args.config)
    core.setup_logging(cfg.output_dir, verbose=args.verbose)
    db = core.DB(cfg.output_dir / "autopilot.sqlite")
    use_lp = bool(cfg.behavior.get("use_learned_patterns", True))
    core.set_use_learned_patterns(use_lp)
    core.load_learned_score_patterns(db)
    return cfg, db


_PROVIDER_TYPES = {
    # xAI Grok (paid)
    "xai":        lambda c: core.Grok(
        api_key=c["api_key"], model=c.get("model", "grok-4"),
        base_url=c.get("base_url", "https://api.x.ai/v1"),
        name="xAI",
    ),
    # Groq — free tier, OpenAI-compatible
    "openai":     lambda c: core.Grok(
        api_key=c["api_key"], model=c.get("model", "gpt-4o-mini"),
        base_url=c.get("base_url", "https://api.openai.com/v1"),
        name="OpenAI",
    ),
    "groq":       lambda c: core.Grok(
        api_key=c["api_key"], model=c.get("model", "llama-3.1-8b-instant"),
        base_url=c.get("base_url", "https://api.groq.com/openai/v1"),
        name="Groq",
    ),
    # OpenRouter — use :free suffix models (e.g. google/gemini-2.0-flash-exp:free)
    "openrouter": lambda c: core.Grok(
        api_key=c["api_key"], model=c.get("model", "google/gemini-2.0-flash-exp:free"),
        base_url="https://openrouter.ai/api/v1",
        extra_headers={
            "HTTP-Referer": "https://github.com/job-autopilot",
            "X-Title": "job-autopilot",
        },
        name="OpenRouter",
    ),
    # Mistral — open-mistral-nemo is the confirmed-free open-source model
    "mistral":    lambda c: core.Grok(
        api_key=c["api_key"], model=c.get("model", "open-mistral-nemo"),
        base_url="https://api.mistral.ai/v1",
        name="Mistral",
    ),
    # Google Gemini — free tier via AI Studio (15 RPM, 1M tokens/day)
    "gemini":     lambda c: core.GeminiClient(
        api_key=c["api_key"], model=c.get("model", "gemini-2.0-flash"),
        name="Gemini",
    ),
}


def _build_llm(cfg: core.Config) -> "core.MultiLLMClient":
    """Build a MultiLLMClient from config.

    Priority order:
      1. llm_providers list (if configured) — used as-is in order
      2. Legacy xai key — single-provider fallback
    """
    providers = []

    if cfg.llm_providers:
        for entry in cfg.llm_providers:
            ptype = (entry.get("type") or "xai").lower()
            if not entry.get("api_key"):
                log.debug("llm_providers: skipping %r — no api_key", ptype)
                continue
            builder = _PROVIDER_TYPES.get(ptype)
            if builder is None:
                log.warning("llm_providers: unknown type %r — skipping", ptype)
                continue
            try:
                p = builder(entry)
                providers.append(p)
                log.info("LLM provider loaded: %s / %s", getattr(p, "name", ptype), entry.get("model", "default"))
            except Exception as e:
                log.warning("llm_providers: failed to init %r: %s", ptype, e)

    if not providers:
        # Fall back to legacy xai key
        key = cfg.xai.get("api_key", "")
        if key and not key.startswith("xai-XXX"):
            providers.append(core.Grok(
                api_key=key,
                model=cfg.xai.get("model", "grok-4"),
                base_url=cfg.xai.get("base_url", "https://api.x.ai/v1"),
            ))
            log.info("LLM: using legacy xai config (model=%s)", cfg.xai.get("model", "grok-4"))

    if providers:
        log.info("LLM: %d provider(s) ready — fallback order: %s",
                 len(providers),
                 ", ".join(getattr(p, "name", type(p).__name__) for p in providers))
    return core.MultiLLMClient(providers) if providers else None


def _clients(cfg: core.Config):
    llm = _build_llm(cfg)
    mailer = core.Gmailer(
        address=cfg.gmail["address"],
        app_password=cfg.gmail["app_password"],
        send=bool(cfg.gmail.get("send", True)),
    )
    return llm, mailer


def _bootstrap(args):
    cfg, db = _bootstrap_state(args)
    grok, mailer = _clients(cfg)
    log.info("resume PDF: %s", cfg.resume_pdf_path)
    return cfg, db, grok, mailer


def _manual_apply_only(cfg: core.Config) -> bool:
    return bool(cfg.behavior.get("manual_apply_only", True))


_DEDUP_SOURCE_PRIORITY = {
    # Prefer employer/direct or primary platform URLs. Aggregators are useful for
    # rich JD text, but they should not replace a better apply link.
    "careers": 0,
    "linkedin": 1,
    "indeed": 2,
    "remoteok": 3,
    "aijobs": 4,
    "mljobs": 4,
    "ycombinator": 5,
    "jobright": 6,
}


def _source_priority(job: dict) -> int:
    return _DEDUP_SOURCE_PRIORITY.get((job.get("source") or "").lower(), 50)


def _merge_duplicate_job(a: dict, b: dict) -> dict:
    """Merge duplicate company/title rows without letting Jobright dominate.

    The returned row keeps the preferred source/apply URL, while borrowing the
    longest description from any duplicate so downstream scoring still has the
    richest JD text available.
    """
    a_rank = _source_priority(a)
    b_rank = _source_priority(b)
    if b_rank < a_rank:
        preferred, other = dict(b), a
    elif b_rank > a_rank:
        preferred, other = dict(a), b
    else:
        # Same source priority: keep the row with more JD text.
        if len(b.get("description") or "") > len(a.get("description") or ""):
            preferred, other = dict(b), a
        else:
            preferred, other = dict(a), b

    if len(other.get("description") or "") > len(preferred.get("description") or ""):
        preferred["description"] = other.get("description") or ""
        preferred["description_source"] = other.get("source") or ""

    for field in ("posted_at", "location", "company", "title", "url"):
        if not preferred.get(field) and other.get(field):
            preferred[field] = other[field]
    return preferred


def _collect_new_jobs(
    cfg: core.Config,
    db: core.DB,
    grok: "core.Grok | None" = None,
    run_tag: str | None = None,
    include_watched: bool = True,
    watched_keywords: list[str] | None = None,
) -> tuple[int, int, int]:
    from datetime import datetime, timezone
    # A caller may pass an explicit run_tag so several chunked invocations
    # (one slice of roles each) all write under ONE logical run. Default keeps
    # the original single-shot behavior: tag this run with "now".
    db.set_run_tag(run_tag or datetime.now(timezone.utc).isoformat())
    role_kws = [r.get("keywords", "") for r in cfg.roles]
    source_limits = cfg.raw.get("source_limits", {}) or {}

    _strict = bool(cfg.behavior.get("strict_freshness", False))
    fresh = sources.search_all(
        cfg.roles, cfg.behavior.get("max_age_hours", 24), source_limits,
        drop_undated=_strict,
        scraper_apis=cfg.raw.get("scraper_apis") or [],
    )
    log.info("found %d jobs from sources (strict_freshness=%s)", len(fresh), _strict)

    # The watched-company scan walks the WHOLE company DB, so in chunked mode it
    # must run exactly once per logical run (caller passes include_watched only
    # on the first chunk) — and with the full keyword set, not just this slice's.
    _skip_careers_scan = bool(cfg.behavior.get("skip_company_careers_scan", False))
    if include_watched and not _skip_careers_scan:
        scan_kws = watched_keywords if watched_keywords is not None else role_kws
        _scan_interval = int(cfg.behavior.get("company_careers_scan_interval_hours", 0) or 0)
        careers = sources.check_watched_companies(
            db, scan_kws, cfg.behavior.get("max_age_hours", 24), source_limits,
            min_scan_interval_hours=_scan_interval)
        log.info("found %d jobs from %d watched companies",
                 len(careers), len(db.list_watched()))
        fresh += careers
    elif _skip_careers_scan:
        log.info("skip_company_careers_scan=true — skipping company website scraping")

    # Cross-source dedup: same (company, normalized_title) from different sources
    # keeps the best apply-link source, while borrowing the longest description.
    _seen_co_title: dict[tuple, dict] = {}
    for j in fresh:
        co = re.sub(r'\s+', ' ', (j.get("company") or "").strip().lower())
        t  = core.normalize_title(j.get("title") or "")
        key = (co, t)
        if key not in _seen_co_title:
            _seen_co_title[key] = j
        else:
            _seen_co_title[key] = _merge_duplicate_job(_seen_co_title[key], j)
    before_dedup = len(fresh)
    fresh = list(_seen_co_title.values())
    if before_dedup != len(fresh):
        log.info("cross-source dedup: %d → %d jobs (removed %d duplicates)",
                 before_dedup, len(fresh), before_dedup - len(fresh))
    if fresh:
        source_mix: dict[str, int] = {}
        for j in fresh:
            src = j.get("source") or "unknown"
            source_mix[src] = source_mix.get(src, 0) + 1
        log.info("source mix after dedup: %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(source_mix.items())))

    blocklist = cfg.filters.get("block_title_keywords", []) or []
    min_match = int(cfg.behavior.get("min_resume_match_score", 35) or 0)
    max_years = core.configured_max_years(cfg.filters)
    auto_watch = cfg.behavior.get("auto_watch_companies", True)
    new_count = 0
    blocked_count = 0
    low_match_count = 0
    for j in fresh:
        jid = j["id"]

        def _mark_skipped(reason: str) -> None:
            if not db.seen(jid):
                j.setdefault("match_score", 0)
                j["skip_reason"] = reason
                db.insert_job(j)
            else:
                db.conn.execute(
                    "UPDATE jobs SET match_score=?, description=? WHERE id=?",
                    (
                        int(j.get("match_score", 0) or 0),
                        (j.get("description") or "")[:8000],
                        jid,
                    ),
                )
                db.conn.commit()
            db.mark(jid, "skipped", reason)

        if core.title_is_blocked(j.get("title", ""), blocklist):
            blocked_count += 1
            _mark_skipped(f"title blocked by filter: {j.get('title', '')[:160]}")
            continue

        _repost_text = (j.get("title", "") + " " + (j.get("description") or "")[:1000])
        if j.get("reposted") or _REPOST_RE.search(_repost_text):
            blocked_count += 1
            _mark_skipped("reposted job")
            continue

        # Skip Jobright-branded jobs that appear on LinkedIn — they are job-board
        # cross-posts, not direct employer postings.
        if j.get("source") == "linkedin" and "jobright" in (j.get("company") or "").lower():
            blocked_count += 1
            log.debug("jobright-on-linkedin filter skipped %r", j.get("title"))
            _mark_skipped("staffing/agency filter: Jobright posting on LinkedIn")
            continue

        # Skip staffing agencies, recruiting firms, and third-party contract shops.
        if cfg.filters.get("block_staffing_agencies", True):
            _staff_blocked, _staff_reason = core.is_staffing_or_agency(
                j.get("company", ""), j.get("description", ""), db,
                title=j.get("title", ""))
            if _staff_blocked:
                blocked_count += 1
                log.debug("staffing filter skipped %r (%s) — %s",
                          j.get("title"), j.get("company"), _staff_reason)
                _mark_skipped(_staff_reason)
                continue

        _blocked_cos = cfg.filters.get("block_companies") or []
        if _blocked_cos:
            _co = (j.get("company") or "").lower()
            _matched_co = next((c for c in _blocked_cos if c.lower() in _co), None)
            if _matched_co:
                blocked_count += 1
                log.debug("company block filter skipped %r (%s)", j.get("title"), j.get("company"))
                _mark_skipped(f"company block filter: {j.get('company')!r}")
                continue

        if cfg.filters.get("block_no_sponsorship", True):
            _visa_blocked, _visa_reason = core.visa_sponsorship_blocked(
                j.get("title", ""), j.get("description", ""))
            if _visa_blocked:
                blocked_count += 1
                log.debug("visa filter skipped %r — %s", j.get("title"), _visa_reason)
                _mark_skipped(_visa_reason)
                continue

        _exp_blocked, _exp_reason = core.experience_requirement_blocked(
            f"{j.get('title', '')} {j.get('description', '')}", max_years)
        if _exp_blocked:
            blocked_count += 1
            log.debug("experience filter skipped %r — %s", j.get("title"), _exp_reason)
            _mark_skipped(_exp_reason)
            continue

        breakdown = core.resume_relevance_breakdown(cfg.base_resume, cfg.roles, j)
        score = int(breakdown["score"])
        # Use keyword fallback summary; LLM will overwrite if available
        j.setdefault("fit_summary", breakdown.get("fit_summary", ""))
        log.debug(
            "pre-enrich %r @ %r: %d (role=%.1f skill=%.1f sem=%.1f onto=%.1f "
            "tech=%.1f title=%.1f tier=%s req_cov=%s caps=%s)",
            j.get("title"), j.get("company"), score,
            breakdown["scorecard_points"], breakdown["resume_skill_points"],
            breakdown["semantic_points"], breakdown["ontology_points"],
            breakdown["technical_overlap_points"], breakdown["title_bonus_points"],
            breakdown["tier"],
            f"{breakdown['required_section_coverage']}%" if breakdown.get("required_section_coverage") is not None else "n/a",
            breakdown["caps"] or "none",
        )

        # ── LinkedIn LLM title gate (before expensive enrichment) ────────────
        # Checks title+company via LLM before fetching the full JD, saving
        # one HTTP request per filtered-out job. Only runs for LinkedIn since
        # Indeed/others already have descriptions from scraping.
        if j.get("source") == "linkedin" and grok is not None:
            if not core.llm_title_match(grok, cfg.base_resume, cfg.roles, j):
                low_match_count += 1
                _mark_skipped("llm-title-filtered")
                continue

        # Enrich all LinkedIn/Indeed rows — always fetch the full posting so
        # filters and scoring run on complete JD text, not search-result snippets.
        _needs_enrich = j.get("source") in {"linkedin", "indeed"}
        _enrich_threshold = int(cfg.behavior.get("detail_enrich_min_score", 20) or 20)
        # LinkedIn already passed the LLM title gate above, so enrich unconditionally.
        if _needs_enrich and (j.get("source") == "linkedin" or score >= _enrich_threshold):
            enriched = sources.enrich_job_details(j)
            if (enriched.get("description") or "") != (j.get("description") or ""):
                j = enriched
                # Re-run staffing filter — title-only rows had no description to
                # check against; the full text may now reveal middleman language.
                if cfg.filters.get("block_staffing_agencies", True):
                    _staff_blocked2, _staff_reason2 = core.is_staffing_or_agency(
                        j.get("company", ""), j.get("description", ""), db,
                        title=j.get("title", ""))
                    if _staff_blocked2:
                        blocked_count += 1
                        _mark_skipped(_staff_reason2)
                        continue
                if cfg.filters.get("block_no_sponsorship", True):
                    _visa_blocked, _visa_reason = core.visa_sponsorship_blocked(
                        j.get("title", ""), j.get("description", ""))
                    if _visa_blocked:
                        blocked_count += 1
                        _mark_skipped(_visa_reason)
                        continue
                _exp_blocked, _exp_reason = core.experience_requirement_blocked(
                    f"{j.get('title', '')} {j.get('description', '')}", max_years)
                if _exp_blocked:
                    blocked_count += 1
                    _mark_skipped(_exp_reason)
                    continue
                post_bd = core.resume_relevance_breakdown(cfg.base_resume, cfg.roles, j)
                score = int(post_bd["score"])
                j["fit_summary"] = post_bd.get("fit_summary", "")
                log.debug(
                    "enrich rescore %r: %d → %d (tier=%s caps=%s)",
                    j.get("title"), int(j.get("match_score", 0) or 0), score,
                    post_bd["tier"], post_bd["caps"] or "none",
                )

        # ── Keyword min_match gate (cheap; runs before any LLM call) ────────────
        # IMPORTANT: the keyword scorer (resume_relevance_breakdown) leans on an
        # ML/CS technical vocabulary, so it under-scores valid non-ML roles
        # (e.g. Industrial Engineer). When an LLM is available AND the job has a
        # real description, we let the LLM be the authoritative scorer instead of
        # dropping the job here — the LLM's score faces min_match right after.
        # The cheap gate still applies when there is no LLM (keyword-only mode).
        j["match_score"] = score
        _will_llm_score = (
            grok is not None
            and len((j.get("description") or "").strip()) >= 150
        )
        if min_match and score < min_match and not _will_llm_score:
            low_match_count += 1
            _mark_skipped(f"score {score}/100 below minimum {min_match}")
            continue

        # ── LLM deep-match scoring (last filter; only survivors reach here) ───
        # Runs AFTER all hard filters + enrichment + keyword min_match so we
        # spend tokens only on jobs the user would actually consider reviewing.
        if (
            grok is not None
            and len((j.get("description") or "").strip()) >= 150
        ):
            llm_result = core.llm_score_resume_match(grok, cfg.base_resume, cfg.roles, j)
            if llm_result is not None:
                old_kw_score = score
                score = llm_result["score"]
                j["fit_summary"] = llm_result["fit_summary"] or j.get("fit_summary", "")
                j["match_score"] = score
                log.debug(
                    "LLM rescore %r @ %r: kw=%d → llm=%d",
                    j.get("title"), j.get("company"), old_kw_score, score,
                )
                if min_match and score < min_match:
                    low_match_count += 1
                    _mark_skipped(f"llm-filtered: score {score}/100 below minimum {min_match}")
                    continue
            else:
                log.debug("LLM skipped for %r, using keyword fallback score=%d", j.get("title"), score)

        # Auto-watch only for jobs that pass all filters.
        if auto_watch:
            company = (j.get("company") or "").strip()
            if company:
                domain = core.slug_domain(company)
                if domain and not any(bd in domain for bd in _BOARD_DOMAINS):
                    db.watch(domain, company)
        was_seen = db.seen(jid)
        db.queue_or_refresh_job(j)
        if not was_seen:
            new_count += 1
    log.info("%d new jobs queued (%d senior titles filtered, %d low resume-match filtered)",
             new_count, blocked_count, low_match_count)
    return new_count, blocked_count, low_match_count


def _export_excel(cfg: "core.Config", db: "core.DB", latest_run_only: bool = False) -> None:
    import shutil
    excel_path = cfg.output_dir / "jobs.xlsx"
    # Import any user feedback from the existing file before overwriting
    n_fb = db.import_excel_feedback(excel_path)
    if n_fb:
        log.info("feedback: imported %d new items from Excel", n_fb)
    db.export_excel(excel_path, latest_run_only=latest_run_only)
    print(f"Excel updated: {excel_path}")
    if cfg.excel_export_name:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%Hh%M")
        downloads = Path.home() / "Downloads" / f"{cfg.excel_export_name}_{ts}.xlsx"
        shutil.copy2(excel_path, downloads)
        print(f"Excel saved: {downloads}")


def _sync_mail_if_enabled(cfg: core.Config, db: core.DB) -> None:
    if not cfg.mail_tracking.get("enabled", False):
        return
    try:
        result = mail_tracker.sync_mail_statuses(cfg, db)
        log.info(
            "mail-sync: scanned=%d classified=%d updated=%d ambiguous=%d unlinked=%d ignored=%d seen=%d",
            result.scanned, result.classified, result.updated, result.ambiguous,
            result.unlinked, result.ignored, result.already_seen,
        )
    except Exception as e:
        log.warning("mail-sync skipped: %s", e)


def _run_feedback_learn(cfg: core.Config, db: core.DB) -> None:
    """Import Excel feedback (labels only — no pattern synthesis)."""
    excel_path = cfg.output_dir / "jobs.xlsx"
    n_fb = db.import_excel_feedback(excel_path)
    if n_fb:
        print(f"  feedback: imported {n_fb} new feedback item(s) from Excel")


def cmd_learn(args) -> int:
    """Import Excel feedback labels and print summary."""
    cfg, db = _bootstrap_state(args)
    excel_path = cfg.output_dir / "jobs.xlsx"

    print("Reading feedback from Excel …")
    n_fb = db.import_excel_feedback(excel_path)
    print(f"  imported: {n_fb} item(s)")

    fb = db.feedback_summary()
    print(f"\nFeedback DB:")
    print(f"  staffing companies flagged : {fb['staffing']}")
    print(f"  companies confirmed OK     : {fb['not_staffing']}")
    print(f"  jobs marked irrelevant     : {fb['not_relevant']}")
    print(f"  jobs marked relevant       : {fb['relevant']}")
    return 0


# ── Label aliases ─────────────────────────────────────────────────────────────
_STAFFING_LABELS  = frozenset({"staffing", "agency", "third-party", "recruiter", "3p"})
_OK_LABELS        = frozenset({"ok", "direct", "not_staffing", "good-company"})
_RELEVANT_LABELS  = frozenset({"relevant", "good", "yes", "y", "keep"})
_IRRELEVANT_LABELS = frozenset({"irrelevant", "bad", "no", "n", "skip", "not_relevant"})


def _find_job(db: core.DB, identifier: str) -> dict | None:
    """Look up a job row by URL, job ID, or partial title match."""
    row = db.conn.execute(
        "SELECT * FROM jobs WHERE url=? OR id=?", (identifier, identifier)
    ).fetchone()
    if row:
        return dict(row)
    # Partial title / company fallback (case-insensitive)
    rows = db.conn.execute(
        "SELECT * FROM jobs WHERE lower(title) LIKE ? OR lower(company) LIKE ? LIMIT 5",
        (f"%{identifier.lower()}%", f"%{identifier.lower()}%"),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    if len(rows) > 1:
        print(f"Ambiguous identifier — {len(rows)} matches. Be more specific or use the job URL.")
        for r in rows:
            print(f"  {r['id']}  {r['title']} @ {r['company']}")
    return None


def cmd_feedback(args) -> int:
    """Record a single feedback label for a job or company from the terminal."""
    cfg, db = _bootstrap_state(args)
    identifier = args.identifier
    label      = (args.label or "").lower().strip()

    if label in _STAFFING_LABELS:
        # Try to resolve company name from a job record first
        job = _find_job(db, identifier)
        company = (job or {}).get("company") or identifier
        domain  = core.slug_domain(company)
        if not domain:
            # identifier might already be a domain/company name
            domain = core.slug_domain(identifier)
            company = identifier
        if not domain:
            print(f"Cannot derive domain from {identifier!r}. Pass the company name or job URL.")
            return 1
        desc = (job or {}).get("description", "") if job else ""
        db.record_company_feedback(domain, company, "staffing", desc)
        print(_green(f"Marked {company!r} as staffing agency (domain={domain})."))
        # Synthesize patterns if we have enough examples
        examples = db.get_staffing_examples()
        if len(examples) >= 3:
            try:
                grok, _ = _clients(cfg)
                n = core.synthesize_staffing_patterns(grok, db)
                if n:
                    print(f"  LLM synthesized {n} new filter pattern(s).")
            except Exception as e:
                log.debug("pattern synthesis skipped: %s", e)

    elif label in _OK_LABELS:
        job = _find_job(db, identifier)
        company = (job or {}).get("company") or identifier
        domain  = core.slug_domain(company) or core.slug_domain(identifier)
        if not domain:
            print(f"Cannot derive domain from {identifier!r}.")
            return 1
        db.record_company_feedback(domain, company, "not_staffing")
        print(_cyan(f"Marked {company!r} as direct employer — will never block it again."))

    elif label in _RELEVANT_LABELS:
        job = _find_job(db, identifier)
        if not job:
            print(f"Job not found: {identifier!r}")
            return 1
        db.record_job_feedback(job["id"], "relevant")
        print(_green(f"Marked '{job['title']} @ {job['company']}' as relevant."))

    elif label in _IRRELEVANT_LABELS:
        job = _find_job(db, identifier)
        if not job:
            print(f"Job not found: {identifier!r}")
            return 1
        db.record_job_feedback(job["id"], "not_relevant")
        print(_yellow(f"Marked '{job['title']} @ {job['company']}' as irrelevant."))

    else:
        print(f"Unknown label {label!r}. Use: staffing | ok | relevant | irrelevant")
        return 1

    _export_excel(cfg, db)
    return 0


def cmd_review(args) -> int:
    """Interactive terminal review: rate queued jobs one by one to train the filter."""
    cfg, db = _bootstrap_state(args)
    limit = getattr(args, "limit", 20) or 20
    include_skipped = getattr(args, "include_skipped", False)

    status_filter = "status IN ('new','ready_for_review')"
    if include_skipped:
        status_filter = "status IN ('new','ready_for_review','skipped')"

    queue = db.conn.execute(
        f"SELECT * FROM jobs WHERE {status_filter} "
        f"ORDER BY match_score DESC, first_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()

    if not queue:
        print("No jobs to review.")
        return 0

    print(_bold(f"\n{'='*70}"))
    print(_bold(f"INTERACTIVE REVIEW — {len(queue)} job(s)"))
    print(_bold("Keys: [y] relevant  [n] irrelevant  [s] staffing  [o] ok/direct  "
                "[b] open browser  [q] quit  Enter = skip"))
    print(_bold(f"{'='*70}\n"))

    new_staffing = 0
    new_feedback = 0

    for i, row in enumerate(queue, 1):
        j = dict(row)
        score   = j.get("match_score", 0)
        title   = j.get("title", "(no title)")
        company = j.get("company", "?")
        url     = j.get("url", "")
        summary = j.get("fit_summary", "") or j.get("skip_reason", "")

        print(f"[{i:02d}/{len(queue)}] {_cyan(f'{score:3d}/100')}  "
              f"{_bold(title)}  @  {company}")
        if summary:
            print(f"       {_yellow(summary[:120])}")
        print(f"       {url}")

        try:
            key = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nReview interrupted.")
            break

        if key == "q":
            break
        elif key == "y":
            db.record_job_feedback(j["id"], "relevant")
            print(_green("  → relevant"))
            new_feedback += 1
        elif key == "n":
            db.record_job_feedback(j["id"], "not_relevant")
            print(_yellow("  → irrelevant"))
            new_feedback += 1
        elif key == "s":
            domain = core.slug_domain(company)
            if domain:
                db.record_company_feedback(domain, company, "staffing",
                                           (j.get("description") or "")[:400])
                db.mark(j["id"], "skipped", f"user review: staffing — {company}")
                print(_red(f"  → {company!r} flagged as staffing agency"))
                new_staffing += 1
                new_feedback += 1
            else:
                print("  → could not derive domain, skipped")
        elif key == "o":
            domain = core.slug_domain(company)
            if domain:
                db.record_company_feedback(domain, company, "not_staffing")
                print(_cyan(f"  → {company!r} confirmed as direct employer"))
                new_feedback += 1
        elif key == "b":
            import webbrowser
            webbrowser.open(url)
            i -= 1  # re-prompt for same job
            try:
                key2 = input("  (browser opened) > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if key2 == "y":
                db.record_job_feedback(j["id"], "relevant"); new_feedback += 1
                print(_green("  → relevant"))
            elif key2 == "n":
                db.record_job_feedback(j["id"], "not_relevant"); new_feedback += 1
                print(_yellow("  → irrelevant"))
            elif key2 == "s":
                domain = core.slug_domain(company)
                if domain:
                    db.record_company_feedback(domain, company, "staffing",
                                               (j.get("description") or "")[:400])
                    db.mark(j["id"], "skipped", f"user review: staffing — {company}")
                    new_staffing += 1; new_feedback += 1
                    print(_red(f"  → {company!r} flagged as staffing"))
        # Enter = skip silently

    print(f"\n{'='*70}")
    print(f"Review done — {new_feedback} label(s) recorded, {new_staffing} new staffing flag(s).")

    if new_feedback > 0:
        _export_excel(cfg, db)

    if new_staffing >= 1 or new_feedback >= 3:
        print("Running LLM pattern synthesis …")
        try:
            grok, _ = _clients(cfg)
            n_s = core.synthesize_staffing_patterns(grok, db)
            n_b, n_p = core.synthesize_score_adjustments(grok, db)
            core.load_learned_score_patterns(db)
            if n_s or n_b or n_p:
                print(f"  synthesized: {n_s} staffing, {n_b} boost, {n_p} penalty pattern(s).")
            else:
                print("  no new patterns — need more examples.")
        except Exception as e:
            print(f"  LLM synthesis skipped: {e}")
    return 0


def cmd_scout(args) -> int:
    cfg, db = _bootstrap_state(args)
    log.info("=== scout start ===")
    try:
        grok, _ = _clients(cfg)
    except Exception:
        grok = None
    new_count, blocked_count, low_match_count = _collect_new_jobs(cfg, db, grok=grok)
    _sync_mail_if_enabled(cfg, db)
    queue = db.new_jobs(cfg.max_per_run)
    print(f"\nnew jobs queued:     {new_count}")
    print(f"senior filtered:     {blocked_count}")
    print(f"low-match filtered:  {low_match_count}")
    print(f"ready for manual review: {len(queue)}")
    print(f"output dir:          {cfg.output_dir}")
    if queue:
        print(f"\n{'='*70}")
        print(_bold(f"QUEUED JOBS FOR MANUAL REVIEW (top {len(queue)} by match score)"))
        print(f"{'='*70}")
        for i, row in enumerate(queue, 1):
            j = dict(row)
            score = j.get('match_score', 0)
            print(_cyan(f"\n[{i:02d}] {score:3d}/100  {j.get('title','?')} @ {j.get('company','?')}"))
            if j.get("fit_summary"):
                print(f"     {_yellow('Fit:')} {j['fit_summary']}")
            print(f"     {j['url']}")
        print(f"\n{'='*70}")
        print("Open jobs.xlsx → 'Fit Summary' column replaces reading the JD.\n")
    _export_excel(cfg, db)
    log.info("=== scout done ===")
    return 0


def cmd_run(args) -> int:
    cfg, db = _bootstrap_state(args)
    log.info("=== run start ===")
    try:
        _run_grok, _ = _clients(cfg)
    except Exception:
        _run_grok = None
    _run_feedback_learn(cfg, db)
    _collect_new_jobs(cfg, db, grok=_run_grok)
    _sync_mail_if_enabled(cfg, db)

    queue = db.new_jobs(cfg.max_per_run)
    if _manual_apply_only(cfg):
        print("\nScrape-only mode is enabled; no applications, browser actions, or emails will run.")
        print(f"jobs ready for manual review: {len(queue)}")
        if queue:
            print(f"\n{'='*70}")
            print(_bold(f"JOBS FOR MANUAL REVIEW (top {len(queue)} by match score)"))
            print(f"{'='*70}")
            for i, row in enumerate(queue, 1):
                j = dict(row)
                score = j.get('match_score', 0)
                print(_cyan(f"\n[{i:02d}] {score:3d}/100  {j.get('title','?')} @ {j.get('company','?')}"))
                if j.get("fit_summary"):
                    print(f"     {_yellow('Fit:')} {j['fit_summary']}")
                print(f"     {j['url']}")
        _export_excel(cfg, db)
        log.info("=== run done: scrape-only, %d ready for manual review ===", len(queue))
        return 0

    if not queue:
        log.info("nothing new to apply to.")
        print("\nNo new jobs to apply to this run.")
        return 0

    grok, mailer = _clients(cfg)

    applied = 0
    results: list[tuple[dict, core.ApplyResult]] = []
    for row in queue:
        job = dict(row)
        print(_cyan(f"\n[score={job.get('match_score', 0):3d}] {job.get('title', '?')} @ {job.get('company', '?')}"))
        print(f"  {job['url']}")
        result = core.apply_to_job(job, cfg, grok, mailer, db)
        log.info("-> %s: %s %s", result.status,
                 result.sent_to or "-", (result.note or result.subject)[:80])
        results.append((job, result))
        if result.status == "applied":
            applied += 1
        elif result.status == "ready_for_review":
            db.mark(job["id"], "ready_for_review")
        elif result.status == "skipped":
            db.mark(job["id"], "skipped")
        elif result.status == "error":
            db.mark(job["id"], "error")
        if result.status == "applied" and applied < cfg.max_per_run:
            time.sleep(cfg.throttle_seconds)

    # ── Post-run job report ──────────────────────────────────────────────
    n_skip = sum(1 for _, r in results if r.status == "skipped")
    n_err  = sum(1 for _, r in results if r.status == "error")
    print(f"\n{'='*62}")
    print(_bold(f"RUN COMPLETE  —  {_green(str(applied)+' applied')}  |  "
                f"{_yellow(str(n_skip)+' skipped')}  |  {_red(str(n_err)+' error')}"))
    print(f"{'='*62}")
    for i, (job, result) in enumerate(results, 1):
        status_label = result.status.upper()
        color = _STATUS_COLOR.get(result.status, lambda t: t)
        title   = job.get("title", "(no title)")
        company = job.get("company", "?")
        url     = job["url"]
        print(color(f"\n[{i:02d}] {status_label:<16}  {title} @ {company}"))
        print(f"     APPLY HERE: {url}")
        if result.jd_summary:
            print(f"     JD: {result.jd_summary}")
        if result.sent_to:
            print(f"     Email sent to: {result.sent_to}")
        if result.note and result.status != "applied":
            print(f"     Note: {result.note}")
    print(f"\n{'='*62}\n")

    _export_excel(cfg, db)
    log.info("=== run done: %d applied ===", applied)
    return 0


def cmd_daemon(args) -> int:
    cfg, _, _, _ = _bootstrap(args)
    log.info("=== daemon start, every %d min ===", cfg.schedule_minutes)

    def _job():
        try:
            cmd_run(args)
        except Exception as e:
            log.exception("run failed: %s", e)

    _job()
    schedule.every(cfg.schedule_minutes).minutes.do(_job)
    if cfg.mail_tracking.get("enabled", False):
        mail_interval = int(cfg.mail_tracking.get("interval_minutes") or 60)

        def _mail_job():
            try:
                cfg2, db2 = _bootstrap_state(args)
                result = mail_tracker.sync_mail_statuses(cfg2, db2)
                _export_excel(cfg2, db2)
                log.info(
                    "mail-sync daemon: scanned=%d classified=%d updated=%d ambiguous=%d unlinked=%d",
                    result.scanned, result.classified, result.updated,
                    result.ambiguous, result.unlinked,
                )
            except Exception as e:
                log.exception("mail-sync daemon failed: %s", e)

        schedule.every(mail_interval).minutes.do(_mail_job)
    while True:
        schedule.run_pending()
        time.sleep(30)


def cmd_status(args) -> int:
    cfg, db = _bootstrap_state(args)
    s = db.stats()
    print(f"jobs seen:         {s['total_jobs']}")
    print(f"  - new:           {s['new']}")
    print(f"  - applied:       {s['applied']}")
    print(f"  - assessment:    {s['assessment']}")
    print(f"  - interview:     {s['interview']}")
    print(f"  - offer:         {s['offer']}")
    print(f"  - rejected:      {s['rejected']}")
    print(f"  - withdrawn:     {s['withdrawn']}")
    print(f"  - ready review:  {s['ready_for_review']}")
    print(f"  - skipped:       {s['skipped']}")
    print(f"  - error:         {s['error']}")
    print(f"watched companies: {s['watched_companies']}")
    print(f"resume PDF:        {cfg.resume_pdf_path}")
    print(f"output dir:        {cfg.output_dir}")
    return 0


def cmd_watch(args) -> int:
    _, db, _, _ = _bootstrap(args)
    db.watch(args.domain.lower(), args.name or args.domain)
    print(f"watching {args.domain}")
    return 0


def cmd_unwatch(args) -> int:
    _, db, _, _ = _bootstrap(args)
    db.unwatch(args.domain.lower())
    print(f"unwatched {args.domain}")
    return 0


def cmd_list_watched(args) -> int:
    _, db, _, _ = _bootstrap(args)
    rows = db.list_watched()
    if not rows:
        print("(no watched companies yet)")
        return 0
    print(f"{'DOMAIN':40} {'NAME':30} LAST CHECKED")
    for r in rows:
        print(f"{r['domain']:40} {(r['name'] or '')[:30]:30} "
              f"{r['last_checked'] or '(never)'}")
    return 0


def cmd_login(args) -> int:
    """Open the browser and let the user log in for authenticated scraping."""
    cfg, _ = _bootstrap_state(args)
    platforms = args.platform if args.platform else ["linkedin", "indeed", "jobright"]
    from applicator import do_platform_login
    for plat in platforms:
        do_platform_login(plat, cfg)
    return 0


def cmd_dry(args) -> int:
    cfg, db = _bootstrap_state(args)
    job = {"id": f"manual:{args.url}", "source": "manual", "url": args.url,
           "title": "", "company": "", "location": "", "description": "",
           "posted_at": ""}
    db.insert_job(job)
    if _manual_apply_only(cfg):
        _export_excel(cfg, db)
        print("\nmanual_apply_only=true; saved URL to Excel without applying.")
        print(f"url: {args.url}")
        return 0
    grok, mailer = _clients(cfg)
    result = core.apply_to_job(job, cfg, grok, mailer, db)
    print(f"\nresult:  {result.status}")
    print(f"sent_to: {result.sent_to}")
    print(f"subject: {result.subject}")
    print(f"pdf:     {result.pdf_path}")
    print(f"note:    {result.note}")
    return 0


def cmd_dashboard(args) -> int:
    import dashboard
    return dashboard.main(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )


def cmd_mail_sync(args) -> int:
    cfg, db = _bootstrap_state(args)
    try:
        result = mail_tracker.sync_mail_statuses(
            cfg,
            db,
            dry_run=args.dry_run,
            force=args.force,
            days=args.days,
            limit=args.limit,
        )
    except Exception as e:
        print(f"mail-sync failed: {e}")
        return 1
    if not args.dry_run:
        _export_excel(cfg, db)
    print(
        "mail-sync: "
        f"scanned={result.scanned} classified={result.classified} "
        f"updated={result.updated} ambiguous={result.ambiguous} "
        f"unlinked={result.unlinked} ignored={result.ignored} "
        f"already_seen={result.already_seen}"
    )
    if args.dry_run:
        print("dry-run only; no DB or Excel changes written.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Local job autopilot.")
    p.add_argument("--config", default=str(HERE / "config.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    ini = sub.add_parser("init", help="Interactive setup for any job seeker (resume, roles, APIs).")
    ini.add_argument("--template", action="store_true", help="Copy config.example.yaml instead of prompting.")
    ini.add_argument("--force", action="store_true", help="Overwrite config.yaml when using --template.")
    ini.set_defaults(fn=cmd_init)
    setup = sub.add_parser("setup", help="Same as init.")
    setup.add_argument("--template", action="store_true")
    setup.add_argument("--force", action="store_true")
    setup.set_defaults(fn=cmd_init)
    sub.add_parser("scout").set_defaults(fn=cmd_scout)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    w = sub.add_parser("watch"); w.add_argument("domain"); w.add_argument("name", nargs="?", default=""); w.set_defaults(fn=cmd_watch)
    u = sub.add_parser("unwatch"); u.add_argument("domain"); u.set_defaults(fn=cmd_unwatch)
    sub.add_parser("list-watched").set_defaults(fn=cmd_list_watched)
    d = sub.add_parser("dry"); d.add_argument("url"); d.set_defaults(fn=cmd_dry)
    ms = sub.add_parser("mail-sync", help="Read mailbox and update job statuses in jobs.xlsx.")
    ms.add_argument("--days", type=int, default=None, help="Lookback window; defaults to mail_tracking.lookback_days.")
    ms.add_argument("--limit", type=int, default=None, help="Max messages per mailbox; defaults to config.")
    ms.add_argument("--dry-run", action="store_true", help="Classify and match without writing DB/Excel changes.")
    ms.add_argument("--force", action="store_true", help="Reprocess messages already recorded in mail_events.")
    ms.set_defaults(fn=cmd_mail_sync)
    lg = sub.add_parser("login", help="Log in to job platforms and save the session for authenticated scraping.")
    lg.add_argument("platform", nargs="*", choices=["linkedin", "indeed", "jobright"],
                    help="Platforms to log in to (default: all three).")
    lg.set_defaults(fn=cmd_login)
    sub.add_parser(
        "learn",
        help="Import Feedback column from jobs.xlsx into DB, then synthesize new filter + score patterns via LLM.",
    ).set_defaults(fn=cmd_learn)

    fb = sub.add_parser(
        "feedback",
        help="Label a job or company from the terminal. "
             "Labels: staffing | ok | relevant | irrelevant",
    )
    fb.add_argument("identifier", help="Job URL, job ID, company name, or domain.")
    fb.add_argument("label",      help="staffing | ok | relevant | irrelevant")
    fb.set_defaults(fn=cmd_feedback)

    rv = sub.add_parser(
        "review",
        help="Interactive terminal review: rate queued jobs one by one to train the scorer.",
    )
    rv.add_argument("--limit", type=int, default=20,
                    help="Max jobs to show per session (default 20).")
    rv.add_argument("--include-skipped", action="store_true",
                    help="Also show previously-skipped jobs.")
    rv.set_defaults(fn=cmd_review)
    dash = sub.add_parser(
        "dashboard",
        help="Open the local web app (setup, find jobs, mailbox, funnel).",
    )
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument("--no-open", action="store_true", help="Do not open a browser.")
    dash.set_defaults(fn=cmd_dashboard)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
