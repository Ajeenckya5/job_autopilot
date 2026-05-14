#!/usr/bin/env python3
"""Job Autopilot - local agent that finds fresh jobs across LinkedIn, Indeed,
Jobright, and watched company careers pages, watches every company it's seen, and
exports scored matches to Excel for manual applications.

Usage:
  ./autopilot.py init                      # create config.yaml from template
  ./autopilot.py scout                     # search + rank only, no applying
  ./autopilot.py run                       # one pass: search + Excel export
  ./autopilot.py daemon                    # loop forever on schedule
  ./autopilot.py status                    # DB stats
  ./autopilot.py watch <domain> [name]     # manually add a company
  ./autopilot.py unwatch <domain>          # remove a company
  ./autopilot.py list-watched              # show watchlist
  ./autopilot.py dry <url>                 # one-shot: score a single URL
  ./autopilot.py login                     # log in to LinkedIn/Indeed/Jobright (run once)
  ./autopilot.py login linkedin            # log in to LinkedIn only
  ./autopilot.py login indeed jobright     # log in to Indeed + Jobright only
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

import schedule

import core
import sources

log = logging.getLogger("autopilot")
HERE = Path(__file__).resolve().parent

# Domains that are job boards / ATS platforms — not real employers to watch.
_BOARD_DOMAINS = frozenset({
    "linkedin.com", "indeed.com", "jobright.ai", "greenhouse.io",
    "lever.co", "workable.com", "bamboohr.com", "glassdoor.com",
    "ziprecruiter.com", "monster.com", "simplyhired.com", "dice.com",
    "builtinnyc.com", "builtin.com", "angel.co",
})


def cmd_init(args) -> int:
    cfg_path = Path(args.config)
    if cfg_path.exists():
        print(f"{cfg_path} already exists.")
        return 1
    shutil.copy(HERE / "config.example.yaml", cfg_path)
    print(f"Wrote {cfg_path}.")
    print("Next: open config.yaml, fill in xai.api_key, gmail credentials,")
    print("point resume_pdf at your PDF (default ~/Downloads/resume.pdf),")
    print("then run:  ./autopilot.py run")
    return 0


def _bootstrap_state(args):
    cfg = core.Config.load(args.config)
    core.setup_logging(cfg.output_dir, verbose=args.verbose)
    db = core.DB(cfg.output_dir / "autopilot.sqlite")
    return cfg, db


def _clients(cfg: core.Config):
    grok = core.Grok(
        api_key=cfg.xai["api_key"],
        model=cfg.xai.get("model", "grok-4"),
        base_url=cfg.xai.get("base_url", "https://api.x.ai/v1"),
        live_search=bool(cfg.xai.get("use_live_search", False)),
    )
    mailer = core.Gmailer(
        address=cfg.gmail["address"],
        app_password=cfg.gmail["app_password"],
        send=bool(cfg.gmail.get("send", True)),
    )
    return grok, mailer


def _bootstrap(args):
    cfg, db = _bootstrap_state(args)
    grok, mailer = _clients(cfg)
    log.info("resume PDF: %s", cfg.resume_pdf_path)
    return cfg, db, grok, mailer


def _manual_apply_only(cfg: core.Config) -> bool:
    return bool(cfg.behavior.get("manual_apply_only", True))


def _collect_new_jobs(cfg: core.Config, db: core.DB) -> tuple[int, int, int]:
    role_kws = [r.get("keywords", "") for r in cfg.roles]
    source_limits = cfg.raw.get("source_limits", {}) or {}

    fresh = sources.search_all(
        cfg.roles, cfg.behavior.get("max_age_hours", 24), source_limits)
    log.info("found %d jobs from sources", len(fresh))

    careers = sources.check_watched_companies(
        db, role_kws, cfg.behavior.get("max_age_hours", 24), source_limits)
    log.info("found %d jobs from %d watched companies",
             len(careers), len(db.list_watched()))
    fresh += careers

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

        if ("repost" in (j.get("title", "") + " " + j.get("description", "")).lower()
                or j.get("reposted")):
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
                j.get("company", ""), j.get("description", ""))
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

        score = core.resume_relevance_score(cfg.base_resume, cfg.roles, j)
        if (
            j.get("source") in {"linkedin", "indeed"}
            and len((j.get("description") or "").strip()) < 300
            and score >= int(cfg.behavior.get("detail_enrich_min_score", 55) or 55)
        ):
            enriched = sources.enrich_job_details(j)
            if (enriched.get("description") or "") != (j.get("description") or ""):
                j = enriched
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
                score = core.resume_relevance_score(cfg.base_resume, cfg.roles, j)
        j["match_score"] = score
        if min_match and score < min_match:
            low_match_count += 1
            _mark_skipped(f"ATS score {score} below minimum {min_match}")
            continue

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


def _export_excel(cfg: "core.Config", db: "core.DB") -> None:
    excel_path = cfg.output_dir / "jobs.xlsx"
    db.export_excel(excel_path)
    print(f"Excel updated: {excel_path}")


def cmd_scout(args) -> int:
    cfg, db = _bootstrap_state(args)
    log.info("=== scout start ===")
    new_count, blocked_count, low_match_count = _collect_new_jobs(cfg, db)
    queue = db.new_jobs(cfg.max_per_run)
    print(f"\nnew jobs queued:     {new_count}")
    print(f"senior filtered:     {blocked_count}")
    print(f"low-match filtered:  {low_match_count}")
    print(f"ready for manual review: {len(queue)}")
    print(f"output dir:          {cfg.output_dir}")
    if queue:
        print(f"\n{'='*62}")
        print(f"QUEUED JOBS FOR MANUAL REVIEW (top {len(queue)} by match score)")
        print(f"{'='*62}")
        for i, row in enumerate(queue, 1):
            j = dict(row)
            print(f"\n[{i:02d}] score={j.get('match_score',0):3d}  "
                  f"{j.get('title','?')} @ {j.get('company','?')}")
            print(f"     {j['url']}")
        print(f"\n{'='*62}")
        print("Open jobs.xlsx and apply manually from the listed URLs.\n")
    _export_excel(cfg, db)
    log.info("=== scout done ===")
    return 0


def cmd_run(args) -> int:
    cfg, db = _bootstrap_state(args)
    log.info("=== run start ===")
    _collect_new_jobs(cfg, db)

    queue = db.new_jobs(cfg.max_per_run)
    if _manual_apply_only(cfg):
        print("\nScrape-only mode is enabled; no applications, browser actions, or emails will run.")
        print(f"jobs ready for manual review: {len(queue)}")
        if queue:
            print(f"\n{'='*62}")
            print(f"JOBS FOR MANUAL REVIEW (top {len(queue)} by match score)")
            print(f"{'='*62}")
            for i, row in enumerate(queue, 1):
                j = dict(row)
                print(f"\n[{i:02d}] score={j.get('match_score',0):3d}  "
                      f"{j.get('title','?')} @ {j.get('company','?')}")
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
        print(f"\n[score={job.get('match_score', 0):3d}] {job.get('title', '?')} @ {job.get('company', '?')}")
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
        if applied < cfg.max_per_run:
            time.sleep(cfg.throttle_seconds)

    # ── Post-run job report ──────────────────────────────────────────────
    n_skip = sum(1 for _, r in results if r.status == "skipped")
    n_err  = sum(1 for _, r in results if r.status == "error")
    print(f"\n{'='*62}")
    print(f"RUN COMPLETE  —  {applied} applied  |  {n_skip} skipped  |  {n_err} error")
    print(f"{'='*62}")
    for i, (job, result) in enumerate(results, 1):
        status_label = result.status.upper()
        title   = job.get("title", "(no title)")
        company = job.get("company", "?")
        url     = job["url"]
        print(f"\n[{i:02d}] {status_label:<16}  {title} @ {company}")
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
    while True:
        schedule.run_pending()
        time.sleep(30)


def cmd_status(args) -> int:
    cfg, db = _bootstrap_state(args)
    s = db.stats()
    print(f"jobs seen:         {s['total_jobs']}")
    print(f"  - new:           {s['new']}")
    print(f"  - applied:       {s['applied']}")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Local job autopilot.")
    p.add_argument("--config", default=str(HERE / "config.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("scout").set_defaults(fn=cmd_scout)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    w = sub.add_parser("watch"); w.add_argument("domain"); w.add_argument("name", nargs="?", default=""); w.set_defaults(fn=cmd_watch)
    u = sub.add_parser("unwatch"); u.add_argument("domain"); u.set_defaults(fn=cmd_unwatch)
    sub.add_parser("list-watched").set_defaults(fn=cmd_list_watched)
    d = sub.add_parser("dry"); d.add_argument("url"); d.set_defaults(fn=cmd_dry)
    lg = sub.add_parser("login", help="Log in to job platforms and save the session for authenticated scraping.")
    lg.add_argument("platform", nargs="*", choices=["linkedin", "indeed", "jobright"],
                    help="Platforms to log in to (default: all three).")
    lg.set_defaults(fn=cmd_login)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
