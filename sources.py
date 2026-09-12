"""Job sources. Each source returns a list of normalized job dicts:
    {id, source, url, title, company, location, description, posted_at}
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("autopilot.sources")

# When SIGALRM fires mid-Playwright-operation, sync_playwright's context manager
# closes the browser while async futures are still pending. asyncio logs those
# TargetClosedError futures at ERROR level as "Future exception was never retrieved."
# The cleanup is correct; this is just noise. Suppress it.
class _PlaywrightTargetClosedFilter(logging.Filter):
    def filter(self, record):
        if record.name == "asyncio" and record.levelno >= logging.ERROR:
            msg = record.getMessage()
            if "TargetClosedError" in msg and "Future exception was never retrieved" in msg:
                return False
        return True

logging.getLogger("asyncio").addFilter(_PlaywrightTargetClosedFilter())

_USER_AGENTS = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"),
    ("Mozilla/5.0 (X11; Linux x86_64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
]
UA = _USER_AGENTS[0]
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_SOURCE_LIMITS = {
    # LinkedIn's guest endpoint is offset-based; it naturally stops around 1 000
    # visible results per query regardless of the cap set here.
    "linkedin_max_jobs_per_role": 5000,
    # Indeed RSS returns 10 results/page; 500 pages = up to 5 000 jobs per role.
    "indeed_max_pages_per_role": 500,
    # Jobright visitor API returns 20 results/page; 1000 pages = up to 20 000.
    "jobright_max_pages_per_role": 1000,
    # Glassdoor job-search pages; 30 results/page.
    "glassdoor_max_pages_per_role": 100,
    # Used only when a watched company has no cached canonical careers_url yet.
    "watched_company_discovery_candidates": 30,
    # Max YC "currently hiring" companies surfaced per role (company-level leads).
    "yc_companies_per_role": 10,
}

# Repost detection — applied across all sources before jobs enter the DB.
_REPOST_RE = re.compile(
    r'\bre[-\s]?post(?:ed|ing|s)?\b'
    r'|\bpreviously\s+(?:posted|listed|advertised)\b'
    r'|\bposting\s+again\b'
    r'|\bopen\s+again\b'
    r'|\bstill\s+(?:hiring|accepting|open)\b.{0,60}?\b(?:posting|position|role)\b',
    re.IGNORECASE,
)


def _limit_int(limits: dict | None, key: str, default: int, minimum: int = 0) -> int:
    raw = (limits or {}).get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _http_get(url: str, timeout: int = 20) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except KeyboardInterrupt:
        # SIGALRM from _playwright_get can surface as KeyboardInterrupt in SSL reads.
        raise requests.exceptions.Timeout(f"interrupted (signal) fetching {url}")


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}, app: {}, csi: () => {}, loadTimes: () => {}};
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory',        {get: () => 8});
Object.defineProperty(navigator, 'platform',            {get: () => 'MacIntel'});
const _OriginalPermissions = navigator.permissions && navigator.permissions.query.bind(navigator.permissions);
if (_OriginalPermissions) {
  navigator.permissions.query = (p) =>
    p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _OriginalPermissions(p);
}
"""

# Chrome 136 Client Hints — sent by real Chrome; required to pass Cloudflare managed challenges.
_CH_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-CH-UA-Arch": '"x86"',
    "Sec-CH-UA-Bitness": '"64"',
    "Sec-CH-UA-Full-Version": '"136.0.7103.93"',
    "Sec-CH-UA-Full-Version-List": (
        '"Chromium";v="136.0.7103.93", '
        '"Google Chrome";v="136.0.7103.93", '
        '"Not-A.Brand";v="99.0.0.0"'
    ),
    "Sec-CH-UA-Platform-Version": '"15.4.0"',
    "Sec-CH-UA-Model": '""',
}


def _is_cf_challenge(html: str) -> bool:
    """Return True if the page is a Cloudflare challenge/security-check page."""
    return bool(
        "INDEED_CLOUDFLARE_STATIC_PAGE" in html
        or "cf-mitigated" in html
        or ("<title>Security Check" in html and "indeed" in html.lower())
        or "challenge-platform" in html
    )


# ---- Per-run Playwright fallback budget --------------------------------------
# Playwright detail-enrichment is the slowest step in the pipeline: launching
# system Chrome and waiting out a Cloudflare managed challenge can cost 8-40s
# for a SINGLE job, and it runs serially. A scheduled run (3x/day, unattended)
# must never stall for hours grinding through these, so we cap both the NUMBER
# of Playwright fallbacks and the total WALL-CLOCK time spent on them per
# process. When either cap is hit, _playwright_get() returns "" immediately and
# the caller degrades gracefully to the HTTP/guest-API text or the search
# snippet. Both caps are env-tunable; a fresh pipeline process resets them
# automatically (module globals reinitialize on import). Set a cap to -1 to
# disable it (e.g. PLAYWRIGHT_MAX_CALLS=-1 for an unbounded manual run).
def _pw_env_num(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() != "" else default
    except (TypeError, ValueError):
        return default


_PW_MAX_CALLS = int(_pw_env_num("PLAYWRIGHT_MAX_CALLS", 12))
_PW_MAX_SECONDS = _pw_env_num("PLAYWRIGHT_MAX_SECONDS", 240.0)
# Per-CALL caps (let an unattended chunk keep Playwright enrichment for accuracy
# without any single call blowing the wall-clock budget). 0 = use the caller's
# timeout arg / the default timeout+40 hard cap.
_PW_CALL_TIMEOUT = int(_pw_env_num("PLAYWRIGHT_CALL_TIMEOUT", 0))
_PW_HARD_TIMEOUT = int(_pw_env_num("PLAYWRIGHT_HARD_TIMEOUT", 0))
_pw_calls_used = 0
_pw_time_used = 0.0


def _pw_budget_allows() -> bool:
    """True if another Playwright fallback is within this run's budget."""
    if _PW_MAX_CALLS >= 0 and _pw_calls_used >= _PW_MAX_CALLS:
        return False
    if _PW_MAX_SECONDS >= 0 and _pw_time_used >= _PW_MAX_SECONDS:
        return False
    return True


def reset_playwright_budget() -> None:
    """Reset the per-run Playwright budget. Optional — a fresh process already
    starts at zero — but lets a long-lived caller start a clean run."""
    global _pw_calls_used, _pw_time_used
    _pw_calls_used = 0
    _pw_time_used = 0.0


def _playwright_get(url: str, timeout: int = 25, wait_selector: str = "") -> str:
    global _pw_calls_used, _pw_time_used
    if not _pw_budget_allows():
        log.info(
            "playwright: per-run budget spent (%d/%s calls, %.0f/%ss) — "
            "skipping fallback for %s",
            _pw_calls_used,
            _PW_MAX_CALLS if _PW_MAX_CALLS >= 0 else "inf",
            _pw_time_used,
            int(_PW_MAX_SECONDS) if _PW_MAX_SECONDS >= 0 else "inf",
            url,
        )
        return ""

    import signal as _signal

    eff_timeout = _PW_CALL_TIMEOUT if _PW_CALL_TIMEOUT > 0 else timeout
    hard_timeout = _PW_HARD_TIMEOUT if _PW_HARD_TIMEOUT > 0 else (eff_timeout + 40)

    def _alarm_handler(sig, frame):
        raise requests.exceptions.Timeout(
            f"playwright hard timeout after {hard_timeout}s"
        )

    _pw_calls_used += 1
    _pw_start = time.monotonic()
    old_handler = _signal.signal(_signal.SIGALRM, _alarm_handler)
    _signal.alarm(hard_timeout)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            _launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            # Prefer system-installed Chrome over bundled Chromium — system Chrome
            # has a genuine fingerprint that passes Cloudflare managed challenges.
            try:
                browser = p.chromium.launch(
                    channel="chrome", headless=True, args=_launch_args
                )
            except Exception:
                browser = p.chromium.launch(headless=True, args=_launch_args)
            ctx = browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
                extra_http_headers=_CH_HEADERS,
            )
            ctx.add_init_script(_STEALTH_JS)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=eff_timeout * 1000)

            # If Cloudflare managed challenge, wait up to 20s for it to auto-resolve
            for _ in range(8):
                html = page.content()
                if not _is_cf_challenge(html):
                    break
                log.debug("playwright: Cloudflare challenge detected — waiting 2.5s for auto-resolve")
                page.wait_for_timeout(2500)
            else:
                html = page.content()
                if _is_cf_challenge(html):
                    log.warning("playwright: Cloudflare challenge did not resolve for %s", url)
                    browser.close()
                    return ""

            # Dismiss cookie/consent overlays common on Indeed and LinkedIn
            for btn_sel in [
                "button[id*='onetrust-accept']",
                "button[aria-label*='Accept']",
                "#onetrust-accept-btn-handler",
                "button:text('Accept all')",
                "button:text('I Accept')",
                "button:text('Continue')",
            ]:
                try:
                    btn = page.locator(btn_sel).first
                    if btn.is_visible(timeout=800):
                        btn.click()
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    pass
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except Exception:
                    pass
            else:
                page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
    finally:
        _signal.alarm(0)
        _signal.signal(_signal.SIGALRM, old_handler)
        _pw_time_used += time.monotonic() - _pw_start
    return html


def _within_age(ts, hours: int) -> bool:
    if not ts:
        return True
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return True
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= timedelta(hours=hours)


def _relative_to_iso(s: str) -> str:
    """Convert Indeed-style relative time strings ('2 days ago', 'Just posted')
    to ISO datetime strings so _within_age can filter them properly."""
    if not s:
        return ""
    sl = s.lower().strip()
    now = datetime.now(timezone.utc)
    if sl in ("just posted", "today", "active today", "posted today"):
        return now.isoformat()
    m = re.match(r"(\d+)\+?\s+hour", sl)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat()
    m = re.match(r"(\d+)\+?\s+day", sl)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat()
    m = re.match(r"(\d+)\+?\s+week", sl)
    if m:
        return (now - timedelta(weeks=int(m.group(1)))).isoformat()
    if "30+" in sl or "month" in sl:
        return (now - timedelta(days=30)).isoformat()
    # Unknown format — return as-is so _within_age treats it as unknown (pass-through)
    return s


def enrich_job_details(job: dict) -> dict:
    """Fetch the full job description for LinkedIn/Indeed rows."""
    desc = job.get("description") or ""
    source = job.get("source")
    try:
        if source == "linkedin":
            detail = _linkedin_detail_text(job)
        elif source == "indeed":
            detail = _indeed_detail_text(job)
        else:
            detail = ""
    except Exception as e:
        log.debug("detail enrichment failed for %s: %s", job.get("id"), e)
        detail = ""
    if detail and len(detail) > len(desc):
        job = dict(job)
        job["description"] = detail[:8000]
    return job


def _linkedin_detail_text(job: dict) -> str:
    jid = (job.get("id") or "").split(":", 1)[-1]
    if not re.fullmatch(r"\d{8,}", jid or ""):
        m = re.search(r"(\d{8,})", job.get("url", ""))
        jid = m.group(1) if m else ""
    if not jid:
        return ""

    # Pass 1: guest API (fast, no JS, no login required)
    try:
        html = _http_get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
            timeout=20,
        )
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(".show-more-less-html__markup")
        if node:
            text = node.get_text(" ", strip=True)
            if len(text.strip()) > 200:
                return re.sub(r"\s+", " ", text)
    except Exception:
        pass

    # Pass 2: Playwright on the public job URL (handles JS-rendered pages and
    # cases where the guest API returns a login wall or empty markup).
    job_url = job.get("url", "")
    if not job_url:
        return ""
    try:
        html = _playwright_get(job_url, timeout=25)
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(
            ".show-more-less-html__markup, "
            "[data-test-id='job-description'], "
            ".jobs-description__content, "
            "#job-details, "
            ".description__text"
        )
        if node:
            text = node.get_text(" ", strip=True)
        else:
            text = soup.get_text(" ", strip=True)
        if len(text.strip()) < 200:
            return ""
        return re.sub(r"\s+", " ", text)
    except Exception:
        return ""


def _indeed_detail_text(job: dict) -> str:
    html = _playwright_get(job.get("url", ""), timeout=25)
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("#jobDescriptionText, [data-testid='jobsearch-JobComponent-description']")
    text = node.get_text(" ", strip=True) if node else soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


# ---------- LinkedIn (guest endpoint) ----------

class LinkedInSource:
    name = "linkedin"
    PAGE_SIZE = 25
    _SLEEP_MIN = 1.2
    _SLEEP_MAX = 2.8
    _MAX_RETRIES = 3
    # f_E: 1=Internship 2=Entry level 3=Associate — skip mid-senior/director/exec.
    # f_JT: F=Full-time C=Contract — skip part-time/volunteer/other.
    _EXPERIENCE_FILTER = "f_E=1%2C2%2C3"
    _JOB_TYPE_FILTER = "f_JT=F%2CC"

    def __init__(self, max_jobs: int | None = None):
        self.max_jobs = max_jobs or DEFAULT_SOURCE_LIMITS["linkedin_max_jobs_per_role"]
        self._ua_index = 0

    def _next_ua(self) -> str:
        ua = _USER_AGENTS[self._ua_index % len(_USER_AGENTS)]
        self._ua_index += 1
        return ua

    def _fetch_page(self, url: str) -> str | None:
        """Fetch one LinkedIn page with retry-after / exponential-backoff on 429."""
        import requests as _req
        for attempt in range(self._MAX_RETRIES + 1):
            headers = {
                "User-Agent": self._next_ua(),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.linkedin.com/jobs/search/",
            }
            try:
                r = _req.get(url, headers=headers, timeout=25)
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 0))
                    # Fail FAST: cap the wait so a rate-limit can't hang the whole
                    # run for 45-180s (the old backoff). A few short retries, then
                    # give up and let the other sources proceed.
                    wait = min(retry_after, 6) if retry_after > 0 else 3 * (attempt + 1)
                    log.warning("linkedin 429 — waiting %ds (attempt %d/%d)",
                                wait, attempt + 1, self._MAX_RETRIES + 1)
                    if attempt == self._MAX_RETRIES:
                        return None
                    time.sleep(wait)
                    continue
                if r.status_code == 400:
                    log.warning("linkedin 400 bad request at start offset — stopping")
                    return None
                r.raise_for_status()
                return r.text
            except KeyboardInterrupt:
                # SIGALRM fired by a prior _playwright_get call can surface as
                # KeyboardInterrupt inside Python's SSL C extension. Treat it as
                # a transient timeout so the run continues rather than crashing.
                log.warning("linkedin fetch interrupted (SIGALRM/signal) on attempt %d — retrying",
                            attempt + 1)
                if attempt < self._MAX_RETRIES:
                    time.sleep(3)
                    continue
                return None
            except Exception as e:
                log.warning("linkedin fetch failed (attempt %d): %s", attempt + 1, e)
                if attempt < self._MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None
        return None

    def _parse_jobs(self, html: str) -> list[dict]:
        """Parse LinkedIn guest API HTML into job dicts. Tries multiple selector
        strategies so the parser survives LinkedIn's occasional markup changes."""
        soup = BeautifulSoup(html, "lxml")
        page_jobs: list[dict] = []

        for li in soup.select("li"):
            # Strategy 1: standard guest-API card selectors
            a = li.select_one("a[href*='/jobs/view']")
            if not a:
                continue
            url_raw = a["href"].split("?", 1)[0]
            jid_m = re.search(r"(\d{8,})", url_raw)
            if not jid_m:
                continue
            jid = jid_m.group(1)

            # Title — try progressively broader selectors
            title_tag = (
                li.select_one(".base-search-card__title")
                or li.select_one("h3")
                or li.select_one(".job-card-list__title")
                or li.select_one("[class*='title']")
            )
            if not title_tag:
                continue

            comp_tag = (
                li.select_one(".base-search-card__subtitle")
                or li.select_one("h4")
                or li.select_one(".job-card-container__company-name")
                or li.select_one("[class*='company']")
            )
            loc_tag = (
                li.select_one(".job-search-card__location")
                or li.select_one(".job-card-container__metadata-item")
                or li.select_one("[class*='location']")
            )
            time_tag = li.select_one("time")

            posted = time_tag["datetime"] if time_tag and time_tag.has_attr("datetime") else ""
            time_text = time_tag.get_text(" ", strip=True).lower() if time_tag else ""

            page_jobs.append({
                "id": f"linkedin:{jid}",
                "source": "linkedin",
                "url": url_raw,
                "title": title_tag.get_text(strip=True),
                "company": comp_tag.get_text(strip=True) if comp_tag else "",
                "location": loc_tag.get_text(strip=True) if loc_tag else "",
                "description": "",
                "posted_at": posted,
                "reposted": "repost" in time_text,
            })
        return page_jobs

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        tpr = f"&f_TPR=r{max_age_hours * 3600}" if max_age_hours <= 168 else ""
        base = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={quote_plus(keywords)}"
            f"&location={quote_plus(location)}"
            f"&{self._EXPERIENCE_FILTER}"
            f"&{self._JOB_TYPE_FILTER}"
            f"{tpr}"
        )
        out: list[dict] = []
        seen_ids: set[str] = set()
        start = 0
        consecutive_empty = 0

        while len(out) < self.max_jobs:
            url = f"{base}&start={start}"
            html = self._fetch_page(url)
            if html is None:
                log.warning("linkedin: stopping at start=%d (fetch failed)", start)
                break

            page_jobs = [j for j in self._parse_jobs(html) if j["id"] not in seen_ids]
            if not page_jobs:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    log.debug("linkedin: %d empty pages in a row — stopping for %r", consecutive_empty, keywords)
                    break
            else:
                consecutive_empty = 0

            for j in page_jobs:
                seen_ids.add(j["id"])
            out.extend(page_jobs)
            start += self.PAGE_SIZE
            time.sleep(random.uniform(self._SLEEP_MIN, self._SLEEP_MAX))

        pages = start // self.PAGE_SIZE
        log.info("linkedin: %d jobs for %r (%d pages)", len(out), keywords, pages)
        return out[:self.max_jobs]


# ---------- Indeed (HTML scrape) ----------

class IndeedSource:
    name = "indeed"
    PAGE_SIZE = 10

    def __init__(self, max_pages: int | None = None):
        self.max_pages = max_pages or DEFAULT_SOURCE_LIMITS["indeed_max_pages_per_role"]

    def _extract_company(self, r: dict) -> str:
        c = r.get("company")
        if isinstance(c, dict):
            return c.get("name") or c.get("displayName") or ""
        for key in ("companyName", "employerName", "employer"):
            v = r.get(key)
            if v:
                return str(v)
        return ""

    def _extract_location(self, r: dict) -> str:
        for key in ("formattedLocation", "jobLocation", "location", "city"):
            v = r.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                parts = [v.get("city", ""), v.get("stateCode", "")]
                loc = ", ".join(p for p in parts if p)
                if loc:
                    return loc
        return ""

    def _build_description(self, r: dict) -> str:
        """Assemble the richest description possible from all known Indeed JSON keys."""
        parts = []
        for key in ("snippet", "jobSnippet", "jobDescription", "description", "summary"):
            v = r.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        highlights = r.get("jobCardRequirementsModel") or {}
        for req_list in highlights.values():
            if isinstance(req_list, list):
                parts.extend(str(x) for x in req_list if x)
        skills = r.get("taxonomyAttributes") or []
        skill_labels = [
            a.get("label", "") for a in skills if isinstance(a, dict)
        ]
        if skill_labels:
            parts.append("Skills: " + ", ".join(filter(None, skill_labels)))
        return " ".join(parts)[:2000]

    def _parse_results_json(self, html: str) -> list[dict]:
        """Try several JSON extraction patterns across Indeed's evolving payload formats."""
        patterns = [
            # 2023-era mosaic provider data
            r'"results"\s*:\s*(\[.*?\])\s*,\s*"sortOptions"',
            # 2024 jobResults key
            r'"jobResults"\s*:\s*(\[.*?\])',
            # Generic jobs array near pagination
            r'"jobs"\s*:\s*(\[.*?\])\s*,\s*"(?:pagination|total)',
            # Full mosaic provider block
            (r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*'
             r'\{[^}]*?"results"\s*:\s*(\[.*?\])'),
            # 2025 initialData variants
            r'"jobCards"\s*:\s*(\[.*?\])\s*,\s*"',
            r'"jobList"\s*:\s*(\[.*?\])\s*[,}]',
            r'"tilesList"\s*:\s*(\[.*?\])\s*[,}]',
            # Inline __NEXT_DATA__ / server state
            r'"jobsInPage"\s*:\s*(\[.*?\])',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.DOTALL)
            if m:
                try:
                    # Truncate at first unmatched ] to avoid huge greedy captures
                    raw_arr = m.group(1)
                    results = json.loads(raw_arr)
                    if isinstance(results, list) and results:
                        return results
                except Exception:
                    continue

        # Last resort: extract all objects that look like job cards (have a jobkey)
        try:
            all_objs = re.findall(
                r'\{"(?:jobkey|jobKey|jk)"\s*:\s*"([^"]{6,})"[^}]{0,2000}\}',
                html, re.DOTALL
            )
            if all_objs:
                log.debug("indeed: fallback jk extraction found %d potential jobs", len(all_objs))
        except Exception:
            pass

        return []

    def _rss_search(self, keywords: str, location: str, fromage: int) -> list[dict]:
        """Primary Indeed scraper via RSS — plain HTTP, no bot detection.
        Tries www RSS first, then mobile RSS as a fallback."""
        out: list[dict] = []
        seen_jks: set[str] = set()
        consecutive_empty = 0

        # Mobile RSS is often less aggressively bot-protected than www RSS.
        _rss_base = "https://www.indeed.com/rss"

        for page in range(self.max_pages):
            start = page * 10
            url = (
                f"{_rss_base}"
                f"?q={quote_plus(keywords)}"
                f"&l={quote_plus(location)}"
                f"&sort=date&fromage={fromage}&start={start}"
            )
            try:
                r = requests.get(
                    url,
                    headers={**HEADERS, "Accept": "application/rss+xml, text/xml, */*"},
                    timeout=20,
                )
                if r.status_code == 403:
                    if _rss_base == "https://www.indeed.com/rss" and page == 0:
                        # Try mobile RSS before giving up
                        log.info("indeed rss: www blocked, trying mobile RSS endpoint")
                        _rss_base = "https://m.indeed.com/rss"
                        url = (
                            f"{_rss_base}"
                            f"?q={quote_plus(keywords)}"
                            f"&l={quote_plus(location)}"
                            f"&sort=date&fromage={fromage}&start={start}"
                        )
                        try:
                            r = requests.get(
                                url,
                                headers={**HEADERS, "Accept": "application/rss+xml, text/xml, */*"},
                                timeout=20,
                            )
                        except Exception as e:
                            log.debug("indeed mobile rss failed: %s", e)
                            break
                        if r.status_code == 403:
                            log.warning(
                                "indeed rss: both www and mobile endpoints 403 — "
                                "will try Playwright fallback"
                            )
                            break
                    else:
                        log.warning(
                            "indeed rss: 403 Cloudflare block on page %d — "
                            "will try Playwright fallback",
                            page,
                        )
                        break
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                log.debug("indeed rss page %d failed: %s", page, e)
                break

            items = root.findall(".//item")
            if not items:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                continue

            consecutive_empty = 0
            page_jobs: list[dict] = []

            for item in items:
                def _t(tag: str) -> str:
                    el = item.find(tag)
                    return (el.text or "").strip() if el is not None else ""

                jk = ""
                for field in ("guid", "link"):
                    m = re.search(r"jk=([a-zA-Z0-9]+)", _t(field))
                    if m:
                        jk = m.group(1)
                        break
                if not jk or jk in seen_jks:
                    continue
                seen_jks.add(jk)

                raw_title = _t("title")
                title, company = raw_title, ""
                for sep in (" - ", " at "):
                    if sep in raw_title:
                        t_part, c_part = raw_title.rsplit(sep, 1)
                        title, company = t_part.strip(), c_part.strip()
                        break

                src_el = item.find("source")
                if src_el is not None and not company:
                    company = (src_el.text or "").strip()

                desc_html = _t("description")
                desc_soup = BeautifulSoup(desc_html, "lxml")

                loc = ""
                for b_tag in desc_soup.find_all(["b", "strong"]):
                    label = b_tag.get_text(strip=True).rstrip(":").lower()
                    sib = b_tag.next_sibling
                    sib_text = str(sib).strip(" :\n") if sib else ""
                    if label == "location" and sib_text:
                        loc = sib_text
                    elif label == "company" and sib_text and not company:
                        company = sib_text

                desc_text = re.sub(r"\s+", " ", desc_soup.get_text(" ", strip=True))[:2000]

                posted_iso = ""
                pub = _t("pubDate")
                if pub:
                    try:
                        posted_iso = parsedate_to_datetime(pub).isoformat()
                    except Exception:
                        pass

                reposted = bool(
                    _REPOST_RE.search(raw_title)
                    or _REPOST_RE.search(desc_text)
                )
                page_jobs.append({
                    "id": f"indeed:{jk}",
                    "source": "indeed",
                    "url": f"https://www.indeed.com/viewjob?jk={jk}",
                    "title": title,
                    "company": company,
                    "location": loc,
                    "description": desc_text,
                    "posted_at": posted_iso,
                    "reposted": reposted,
                })

            if not page_jobs:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                out.extend(page_jobs)

            time.sleep(random.uniform(0.5, 1.5))

        return out

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        fromage = max(1, max_age_hours // 24)

        # Primary: RSS (plain HTTP, not bot-detected)
        out = self._rss_search(keywords, location, fromage)
        if out:
            log.info("indeed: %d jobs for %r (RSS)", len(out), keywords)
            return out

        log.info("indeed: RSS empty for %r — falling back to Playwright", keywords)

        base = (
            f"https://www.indeed.com/jobs?q={quote_plus(keywords)}"
            f"&l={quote_plus(location)}&fromage={fromage}&sort=date"
        )
        out = []
        seen_jks: set[str] = set()
        consecutive_empty = 0
        pages_fetched = 0

        for page in range(self.max_pages):
            start = page * self.PAGE_SIZE
            url = f"{base}&start={start}"
            try:
                html = _playwright_get(
                    url, timeout=25,
                    wait_selector="[data-jk], .job_seen_beacon, .jobCard",
                )
            except Exception as e:
                log.warning("indeed fetch failed at page %d: %s", page, e)
                break

            page_jobs: list[dict] = []
            results = self._parse_results_json(html)
            if not results and page == 0:
                log.debug(
                    "indeed: JSON extraction found nothing on page 0 (html len=%d, "
                    "snippet=%r)", len(html), html[:500]
                )
            for r in results:
                if not isinstance(r, dict):
                    continue
                jk = r.get("jobkey") or r.get("jobKey") or r.get("id") or ""
                if not jk or jk in seen_jks:
                    continue
                seen_jks.add(jk)
                rel_time = r.get("formattedRelativeTime") or r.get("age") or ""
                page_jobs.append({
                    "id": f"indeed:{jk}",
                    "source": "indeed",
                    "url": f"https://www.indeed.com/viewjob?jk={jk}",
                    "title": (
                        r.get("displayTitle") or r.get("normTitle")
                        or r.get("title") or ""
                    ),
                    "company": self._extract_company(r),
                    "location": self._extract_location(r),
                    "description": self._build_description(r),
                    "posted_at": _relative_to_iso(rel_time),
                    "reposted": bool(r.get("repost")) or "repost" in rel_time.lower(),
                })

            # HTML fallback — picks up jobs when the JSON payload is missing
            if not page_jobs:
                soup = BeautifulSoup(html, "lxml")
                for card in soup.select("[data-jk], a[data-jk]"):
                    jk = card.get("data-jk") or ""
                    if not jk or jk in seen_jks:
                        continue
                    seen_jks.add(jk)
                    title_el = (
                        card.select_one("[class*='jobTitle'], h2, h3")
                        or card
                    )
                    comp_el = card.select_one("[class*='companyName'], [data-testid*='company']")
                    loc_el = card.select_one("[class*='companyLocation'], [data-testid*='location']")
                    snippet_el = card.select_one("[class*='snippet'], [class*='summary']")
                    page_jobs.append({
                        "id": f"indeed:{jk}",
                        "source": "indeed",
                        "url": f"https://www.indeed.com/viewjob?jk={jk}",
                        "title": title_el.get_text(strip=True) if title_el else "",
                        "company": comp_el.get_text(strip=True) if comp_el else "",
                        "location": loc_el.get_text(strip=True) if loc_el else "",
                        "description": snippet_el.get_text(strip=True) if snippet_el else "",
                        "posted_at": "",
                    })

            if not page_jobs:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                out.extend(page_jobs)

            pages_fetched += 1
            time.sleep(random.uniform(1.5, 3.0))

        log.info("indeed: %d jobs for %r (%d pages)", len(out), keywords, pages_fetched)
        return out

# ---------- Jobright AI (public visitor API + Next.js fallback) ----------

class JobrightSource:
    name = "jobright"
    PAGE_SIZE = 20

    def __init__(self, max_pages: int | None = None):
        self.max_pages = max_pages or DEFAULT_SOURCE_LIMITS["jobright_max_pages_per_role"]

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        total_jobs = 0
        for page in range(self.max_pages):
            position = page * self.PAGE_SIZE
            try:
                items, total_jobs = self._visitor_search_page(keywords, max_age_hours, position)
            except Exception as e:
                log.warning("jobright API fetch failed at page %d: %s", page + 1, e)
                if page == 0:
                    return self._html_search(keywords, location, max_age_hours)
                break
            if not items:
                break
            item_ids = [
                f"jobright:{(item.get('jobResult') or {}).get('jobId')}"
                for item in items
                if (item.get("jobResult") or {}).get("jobId")
            ]
            if item_ids and all(item_id in seen for item_id in item_ids):
                break
            for job in self._items_to_jobs(items, location, max_age_hours):
                if job["id"] in seen:
                    continue
                seen.add(job["id"])
                out.append(job)
            if len(items) < self.PAGE_SIZE:
                break
            if total_jobs and position + len(items) >= total_jobs:
                break
            time.sleep(0.8)
        log.info("jobright: %d jobs for %r (%d pages, total=%s)",
                 len(out), keywords, page + 1, total_jobs or "?")
        return out

    def _visitor_search_page(self, keywords: str, max_age_hours: int,
                             position: int) -> tuple[list[dict], int]:
        params = {
            "lite": "false",
            "count": str(self.PAGE_SIZE),
            "position": str(position),
            "searchType": "job_title",
            "sortCondition": "RECOMMENDED",
        }
        payload: dict[str, object] = {
            "searchType": "job_title",
            "country": "US",
            "jobTaxonomyList": [{"taxonomyId": "00-00-00", "title": keywords}],
            "locations": [],
            "companies": [],
            "excludedCompanies": [],
            "jobTypes": [],
            "seniority": [],
            "workModel": [],
            "count": self.PAGE_SIZE,
            "position": position,
            "sortCondition": "RECOMMENDED",
            "lite": False,
        }
        if max_age_hours <= 24:
            payload["daysAgo"] = "1"
        url = "https://jobright.ai/swan/recommend/visitor-search?" + urlencode(params)
        headers = dict(HEADERS)
        headers.update({
            "Content-Type": "application/json",
            "Origin": "https://jobright.ai",
            "Referer": "https://jobright.ai/jobs/search",
        })
        # Fail fast: jobright's visitor API sometimes stalls. A short timeout lets
        # us fall back to the HTML search on page 0 instead of hanging 30s/page and
        # starving the other sources of their time budget. Tunable via env.
        _jr_to = int(os.environ.get("JOBRIGHT_TIMEOUT", "14"))
        r = requests.post(url, headers=headers, json=payload, timeout=_jr_to)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(data.get("errorMsg") or data.get("result") or "request failed")
        result = data.get("result") or {}
        return result.get("jobList") or [], int(result.get("jobNum") or 0)

    def _html_search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        params: dict[str, str] = {
            "searchType": "job_title",
            "value": keywords,
            "country": "US",
        }
        if max_age_hours <= 24:
            params["daysAgo"] = "1"
        url = "https://jobright.ai/jobs/search?" + urlencode(params)
        try:
            html = _http_get(url, timeout=30)
        except Exception as e:
            log.warning("jobright fetch failed: %s", e)
            return []

        data = self._next_data(html)
        jobs = (((data.get("props") or {}).get("pageProps") or {}).get("jobList") or [])
        out = self._items_to_jobs(jobs, location, max_age_hours)
        log.info("jobright: %d jobs for %r (HTML fallback)", len(out), keywords)
        return out

    def _items_to_jobs(self, jobs: list[dict], location: str,
                       max_age_hours: int) -> list[dict]:
        out: list[dict] = []
        for item in jobs:
            jr = item.get("jobResult") or {}
            cr = item.get("companyResult") or {}
            jid = jr.get("jobId")
            if not jid:
                continue
            posted = self._posted_at(jr.get("publishTime"))
            if posted and not _within_age(posted, max_age_hours):
                continue
            notes = ((item.get("jobNotes") or {}).get("notesMap") or {})
            min_years = notes.get("min_required_years") or ""

            requirements = jr.get("requirements") or []
            if not isinstance(requirements, list):
                requirements = []

            recommendation_tags = jr.get("recommendationTags") or []
            if not isinstance(recommendation_tags, list):
                recommendation_tags = []

            # Pull every text field Jobright exposes so the scorer has real signal
            skill_tags: list[str] = []
            for tag_list in [
                jr.get("skillTags"), jr.get("techTags"),
                jr.get("jobTags"), jr.get("requiredSkills"),
            ]:
                if isinstance(tag_list, list):
                    skill_tags.extend(str(t) for t in tag_list if t)

            qualification = jr.get("qualification") or jr.get("qualificationSummary") or ""
            responsibility = jr.get("responsibility") or jr.get("jobResponsibilities") or ""

            desc_bits = [
                jr.get("jobNlpTitle") or "",
                jr.get("employmentType") or "",
                jr.get("workModel") or "",
                jr.get("jobSeniority") or "",
                jr.get("jobSummary") or "",
                qualification,
                responsibility,
                " ".join(str(x) for x in requirements),
                " ".join(skill_tags),
                " ".join(str(x) for x in recommendation_tags),
                f"{min_years}+ years exp" if min_years else "",
            ]
            description = " ".join(b for b in desc_bits if b)

            job_title_raw = jr.get("jobTitle") or ""
            reposted = bool(
                jr.get("repost")
                or jr.get("isRepost")
                or _REPOST_RE.search(job_title_raw)
                or _REPOST_RE.search(description)
            )
            out.append({
                "id": f"jobright:{jid}",
                "source": "jobright",
                "url": f"https://jobright.ai/jobs/info/{jid}",
                "title": job_title_raw,
                "company": cr.get("companyName") or "",
                "location": jr.get("jobLocation") or location,
                "description": description[:6000],
                "posted_at": posted,
                "reposted": reposted,
            })
        return out

    def _next_data(self, html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            return {}
        try:
            return json.loads(tag.string)
        except Exception:
            return {}

    def _posted_at(self, raw: str | None) -> str:
        if not raw:
            return ""
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            ).isoformat()
        except ValueError:
            return raw

# ---------- RemoteOK (public JSON API, no auth) ----------

class RemoteOKSource:
    """RemoteOK public API — free, no auth. Strong signal for remote ML/AI roles."""
    name = "remoteok"
    API_URL = "https://remoteok.com/api"

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        try:
            hdrs = dict(HEADERS)
            hdrs["Accept"] = "application/json"
            r = requests.get(self.API_URL, headers=hdrs, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("remoteok: fetch failed: %s", e)
            return []

        kw_terms = [t.lower() for t in keywords.split() if len(t) > 2]
        out: list[dict] = []
        for j in data:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            epoch = j.get("epoch") or 0
            if epoch:
                posted_dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
                if not _within_age(posted_dt.isoformat(), max_age_hours):
                    continue
            title = (j.get("position") or "").strip()
            company = (j.get("company") or "").strip()
            tags = j.get("tags") or []
            desc = (j.get("description") or "").strip()
            blob = f"{title} {company} {' '.join(str(t) for t in tags)} {desc}".lower()
            if kw_terms and not any(t in blob for t in kw_terms):
                continue
            job_url = j.get("url") or f"https://remoteok.com/remote-jobs/{j['id']}"
            posted_iso = (
                datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
                if epoch else ""
            )
            out.append({
                "id": f"remoteok:{j['id']}",
                "source": "remoteok",
                "url": job_url,
                "title": title,
                "company": company,
                "location": "Remote",
                "description": (
                    f"Skills: {', '.join(str(t) for t in tags)}\n\n{desc}"
                )[:6000],
                "posted_at": posted_iso,
            })
        log.info("remoteok: %d jobs for %r", len(out), keywords)
        return out


# ---------- Glassdoor (HTML / __NEXT_DATA__ scrape + Playwright fallback) ----------

class GlassdoorSource:
    """Glassdoor job search.  Tries plain HTTP first; if Cloudflare blocks it,
    falls back to Playwright.  Results are parsed from the embedded __NEXT_DATA__
    JSON blob or from HTML job-card elements."""
    name = "glassdoor"
    PAGE_SIZE = 30

    def __init__(self, max_pages: int | None = None):
        self.max_pages = max_pages or 100

    def _build_url(self, keywords: str, from_age_days: int, page: int) -> str:
        kw_slug = re.sub(r"[^a-z0-9]+", "-", keywords.lower()).strip("-")
        kw_len = len(kw_slug)
        offset = (page - 1) * self.PAGE_SIZE
        return (
            f"https://www.glassdoor.com/Job/{kw_slug}-jobs-SRCH_KO0,{kw_len}.htm"
            f"?fromAge={from_age_days}&sort.sortType=date&sort.ascending=false&start={offset}"
        )

    def _cffi_session(self):
        """Return a curl_cffi Session that impersonates Chrome 136.
        Glassdoor's Cloudflare blocks plain requests and headless Playwright
        but passes curl_cffi TLS-fingerprint impersonation."""
        try:
            from curl_cffi import requests as _cffi
            return _cffi.Session(impersonate="chrome136")
        except ImportError:
            return None

    def _parse_html(self, html: str) -> list[dict]:  # noqa: F811  (redefines above)
        """Parse Glassdoor search-result HTML.

        Glassdoor embeds job data as a doubly-JSON-encoded string inside a
        <script> tag.  The fields use the pattern  \\"key\\":\\"value\\"
        (one literal backslash + one quote) in the raw HTML text.
        We therefore work on r.text directly — never run the HTML through
        BeautifulSoup before extracting these fields, as BS4 may re-escape
        the string and break the backslash-quote patterns.
        """
        ids    = re.findall(r'\\"listingId\\":(\d+)', html)
        titles = re.findall(r'\\"jobTitleText\\":\\"([^\\"]+)\\"', html)
        comps  = re.findall(r'\\"employerNameFromSearch\\":\\"([^\\"]+)\\"', html)
        locs   = re.findall(r'\\"locationName\\":\\"([^\\"]+)\\"', html)
        ages   = re.findall(r'\\"ageInDays\\":(\d+)', html)

        if not ids:
            return []

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        out = []
        for i, jid in enumerate(ids):
            title = titles[i] if i < len(titles) else ""
            comp  = comps[i]  if i < len(comps)  else ""
            loc   = locs[i]   if i < len(locs)   else ""
            age   = int(ages[i]) if i < len(ages) else 0
            posted_iso = (now - timedelta(days=age)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            reposted = bool(_REPOST_RE.search(title))
            out.append({
                "id": f"glassdoor:{jid}",
                "source": "glassdoor",
                "url": f"https://www.glassdoor.com/job-listing/j?jl={jid}",
                "title": title,
                "company": comp,
                "location": loc,
                "description": "",
                "posted_at": posted_iso,
                "reposted": reposted,
            })
        return out

    def _warm_cffi_session(self, sess) -> bool:
        """Hit the Glassdoor homepage to receive the __cf_bm cookie.
        The homepage itself may return 403, but the cookie is still set
        and subsequent search requests then return 200."""
        try:
            sess.get(
                "https://www.glassdoor.com",
                headers={"Accept-Language": "en-US,en;q=0.9"},
                timeout=15,
            )
            return True
        except Exception as e:
            log.debug("glassdoor: warmup request failed: %s", e)
            return False

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        from_age_days = max(1, max_age_hours // 24)
        out: list[dict] = []
        seen: set[str] = set()
        consecutive_empty = 0
        sess = self._cffi_session()

        # Warm the session once so Cloudflare sets the __cf_bm cookie.
        if sess is not None:
            self._warm_cffi_session(sess)
            time.sleep(random.uniform(0.8, 1.5))

        for page in range(1, self.max_pages + 1):
            url = self._build_url(keywords, from_age_days, page)
            html = ""

            if sess is not None:
                try:
                    r = sess.get(
                        url,
                        headers={
                            "Accept-Language": "en-US,en;q=0.9",
                            "Referer": "https://www.glassdoor.com/",
                        },
                        timeout=25,
                    )
                    if r.status_code == 200 and "Security" not in r.text[:200]:
                        html = r.text
                    else:
                        log.debug("glassdoor: cffi page %d status=%d", page, r.status_code)
                except Exception as e:
                    log.debug("glassdoor: cffi page %d failed: %s", page, e)

            if not html:
                log.debug("glassdoor: page %d using playwright fallback", page)
                html = _playwright_get(
                    url, timeout=30,
                    wait_selector="[data-id], li[class*='JobsList_jobListItem']",
                )

            if not html:
                break

            page_jobs = self._parse_html(html)
            new_jobs = [j for j in page_jobs if j["id"] not in seen]
            if not new_jobs:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                continue

            consecutive_empty = 0
            for j in new_jobs:
                seen.add(j["id"])
                out.append(j)

            time.sleep(random.uniform(1.5, 3.0))

        log.info("glassdoor: %d jobs for %r (%d pages)", len(out), keywords, page)
        return out


def _compact_age_to_iso(s: str) -> str:
    """Parse compact relative-age tokens used by aggregator boards
    ('1d', '4D', '2W', '13h', '1mo', 'today', 'new') into an ISO timestamp so
    the standard age window (_within_age) can filter them. Returns '' when the
    token is unparseable (the job then passes the window as unknown-age)."""
    if not s:
        return ""
    sl = s.strip().lower()
    now = datetime.now(timezone.utc)
    if sl in ("today", "just posted", "just now", "new", "0d", "0h"):
        return now.isoformat()
    matches = re.findall(r"(\d+)\s*(mo|h|d|w)\b", sl)
    if not matches:
        return ""
    n_str, unit = matches[-1]  # rightmost token is the age (avoids salary digits)
    n = int(n_str)
    if unit == "h":
        delta = timedelta(hours=n)
    elif unit == "d":
        delta = timedelta(days=n)
    elif unit == "w":
        delta = timedelta(weeks=n)
    else:  # "mo"
        delta = timedelta(days=30 * n)
    return (now - delta).isoformat()


# Aggregator boards serve the SAME listing regardless of role keyword, so each
# board is fetched+parsed at most once per process and cached here; search_all()
# then applies the per-role keyword filter and the age window to the cached rows.
_AIJOBS_CACHE: list[dict] | None = None
_MLJOBS_CACHE: list[dict] | None = None


class AijobsSource:
    """aijobs.ai — server-rendered AI/ML/Data-Science job board. Parsed once and
    cached. Per-job posted_at comes from the card's relative-age badge, so the
    freshness window filters these exactly like every other source."""
    name = "aijobs"
    LIST_URL = "https://aijobs.ai/jobs"

    def _parse(self) -> list[dict]:
        global _AIJOBS_CACHE
        if _AIJOBS_CACHE is not None:
            return _AIJOBS_CACHE
        out: list[dict] = []
        try:
            html = _http_get(self.LIST_URL, timeout=25)
        except Exception as e:
            log.warning("aijobs: fetch failed: %s", e)
            _AIJOBS_CACHE = []
            return _AIJOBS_CACHE
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        for a in soup.select("a.jobcardStyle1"):
            href = a.get("href") or ""
            if "/job/" not in href or href in seen:
                continue
            seen.add(href)
            title_el = a.select_one(".tw-text-lg.tw-font-medium") or a.find("h3")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            comp_el = a.select_one(".iconbox-content .tw-card-title") \
                or a.select_one(".iconbox-content .tw-text-base")
            company = comp_el.get_text(" ", strip=True) if comp_el else ""
            loc_el = a.select_one(".iconbox-content .tw-location")
            location = loc_el.get_text(" ", strip=True) if loc_el else ""
            age_el = a.select_one("div.tw-text-sm")
            posted = _compact_age_to_iso(age_el.get_text(strip=True)) if age_el else ""
            if not title or not company:
                continue
            url = href if href.startswith("http") else urljoin("https://aijobs.ai", href)
            slug = href.rstrip("/").split("/")[-1]
            out.append({
                "id": f"aijobs:{slug}",
                "source": "aijobs",
                "url": url,
                "title": title,
                "company": company,
                "location": location,
                "description": "",
                "posted_at": posted,
            })
        _AIJOBS_CACHE = out
        log.info("aijobs: parsed %d jobs", len(out))
        return out

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        # Full listing; search_all() applies keyword + age filters and dedups.
        return list(self._parse())


class MljobsSource:
    """mljobs.io — server-rendered board curating ML/AI roles at top labs
    (OpenAI, Anthropic, DeepMind, Mistral, ...). A small fixed set of category
    pages is fetched once per process and unioned; posted_at comes from each
    row's relative-age badge so the freshness window applies normally."""
    name = "mljobs"
    BASE = "https://mljobs.io"
    CATEGORY_PATHS = (
        "/machine-learning", "/ai-engineer", "/applied-ai", "/data-science",
    )

    def _parse(self) -> list[dict]:
        global _MLJOBS_CACHE
        if _MLJOBS_CACHE is not None:
            return _MLJOBS_CACHE
        by_id: dict[str, dict] = {}
        for path in self.CATEGORY_PATHS:
            try:
                html = _http_get(self.BASE + path, timeout=25)
            except Exception as e:
                log.warning("mljobs: fetch %s failed: %s", path, e)
                continue
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/jobs/" not in href:
                    continue
                h3 = a.find("h3")
                title = h3.get_text(" ", strip=True) if h3 else ""
                comp_el = a.select_one("span.font-normal")
                company = comp_el.get_text(" ", strip=True) if comp_el else ""
                location = ""
                if comp_el:
                    loc_el = comp_el.find_next_sibling("span")
                    if loc_el:
                        location = loc_el.get_text(" ", strip=True)
                posted = _compact_age_to_iso(a.get_text(" ", strip=True))
                if not title or not company:
                    continue
                url = href if href.startswith("http") else urljoin(self.BASE, href)
                slug = href.rstrip("/").split("/")[-1]
                by_id.setdefault(f"mljobs:{slug}", {
                    "id": f"mljobs:{slug}",
                    "source": "mljobs",
                    "url": url,
                    "title": title,
                    "company": company,
                    "location": location,
                    "description": "",
                    "posted_at": posted,
                })
            time.sleep(1)
        _MLJOBS_CACHE = list(by_id.values())
        log.info("mljobs: parsed %d jobs", len(_MLJOBS_CACHE))
        return _MLJOBS_CACHE

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        return list(self._parse())


class IndeedApiSource:
    """Indeed jobs sourced through the official Indeed API (via the Cowork MCP).

    Indeed's public HTML is Cloudflare-protected (HTTP 403 from scrapers), so the
    Cowork orchestrator calls the Indeed API tool, normalizes the results, and
    writes them to a JSON cache. This source simply reads that cache. The path is
    given by the INDEED_CACHE_JSON env var (per-track, so the ML and Ops pipelines
    never share an Indeed cache).

    Cache format: a JSON list of normalized job dicts, or {"jobs": [ ... ]}.
    Each dict: {id, source, url, title, company, location, description, posted_at}.
    """
    name = "indeed"

    def __init__(self, cache_path: str | None = None,
                 fallback_max_pages: int | None = None):
        self._cache_path = cache_path or os.environ.get("INDEED_CACHE_JSON", "")
        self._loaded: list[dict] | None = None
        self._fallback = IndeedSource(max_pages=fallback_max_pages)

    def _load(self) -> list[dict]:
        if self._loaded is not None:
            return self._loaded
        self._loaded = []
        path = self._cache_path
        if not path or not os.path.isfile(path):
            log.info("indeed-api: no cache at %r — using direct Indeed fallback", path)
            return self._loaded
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            log.warning("indeed-api: failed to read cache %s: %s", path, e)
            return self._loaded
        if isinstance(data, dict):
            data = data.get("jobs", [])
        out = []
        for j in data or []:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            jj = dict(j)
            jj.setdefault("source", "indeed")
            jj.setdefault("location", "")
            jj.setdefault("description", "")
            jj.setdefault("posted_at", "")
            out.append(jj)
        self._loaded = out
        log.info("indeed-api: loaded %d cached jobs from %s", len(out), path)
        return out

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        # Return the full cache; search_all() applies the per-role keyword filter
        # and the age window, and dedups by id across roles.
        cached = list(self._load())
        if cached:
            return cached
        return self._fallback.search(keywords, location, max_age_hours)


# Module-level cache so the YC dataset (≈6k companies) is fetched at most once
# per process even though search_all() calls each source once per role.
_YC_HIRING: list[dict] | None = None
_YC_API_URL = "https://yc-oss.github.io/api/companies/hiring.json"


def _load_yc_companies() -> list[dict]:
    global _YC_HIRING
    if _YC_HIRING is not None:
        return _YC_HIRING
    # 1) Explicit cache file (written by the orchestrator) wins.
    cache_path = os.environ.get("YC_CACHE_JSON", "")
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("companies", data.get("jobs", []))
            _YC_HIRING = [c for c in (data or []) if isinstance(c, dict)]
            log.info("ycombinator: loaded %d companies from cache %s",
                     len(_YC_HIRING), cache_path)
            return _YC_HIRING
        except Exception as e:
            log.warning("ycombinator: cache read failed (%s); trying live fetch", e)
    # 2) Live fetch of the daily-updated public dataset.
    try:
        hdrs = dict(HEADERS)
        hdrs["Accept"] = "application/json"
        r = requests.get(_YC_API_URL, headers=hdrs, timeout=30)
        r.raise_for_status()
        data = r.json()
        _YC_HIRING = [c for c in (data or []) if isinstance(c, dict)]
        log.info("ycombinator: fetched %d hiring companies", len(_YC_HIRING))
    except Exception as e:
        log.warning("ycombinator: fetch failed: %s", e)
        _YC_HIRING = []
    return _YC_HIRING


class YCombinatorSource:
    """Y Combinator startups that are currently hiring, matched to each role.

    Data comes from the open yc-oss dataset (daily snapshot of YC's Algolia index),
    which is company-level — it has no per-posting timestamp, so these entries are
    not subject to the freshness window (posted_at left blank) and are capped per
    role to keep the digest tight. Each entry links to the company's YC jobs page.
    """
    name = "ycombinator"

    def __init__(self, per_role_limit: int = 10, us_only: bool = True):
        self.per_role_limit = per_role_limit
        self.us_only = us_only

    @staticmethod
    def _is_us(company: dict) -> bool:
        locs = company.get("all_locations") or ""
        if isinstance(locs, list):
            locs = " ".join(str(x) for x in locs)
        blob = f"{locs} {company.get('regions') or ''}".lower()
        if not blob.strip():
            return True  # unknown location — don't exclude
        if "remote" in blob:
            return True
        return any(t in blob for t in (
            "united states", "usa", "u.s.", ", ca", ", ny", ", tx", ", wa",
            ", ma", ", il", ", co", "san francisco", "new york", "boston",
            "seattle", "austin", "los angeles", "chicago", "america",
        ))

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        companies = _load_yc_companies()
        if not companies:
            return []
        kw_terms = [t for t in re.split(r"\W+", keywords.lower()) if len(t) > 2]
        if not kw_terms:
            return []
        scored: list[tuple[int, dict]] = []
        for c in companies:
            if not c.get("isHiring", True):
                continue
            if self.us_only and not self._is_us(c):
                continue
            name = (c.get("name") or "").strip()
            if not name:
                continue
            tags = c.get("tags") or []
            if isinstance(tags, list):
                tags_s = " ".join(str(t) for t in tags)
            else:
                tags_s = str(tags)
            blob = " ".join(str(x) for x in (
                name, c.get("one_liner") or "", c.get("long_description") or "",
                c.get("industry") or "", c.get("subindustry") or "", tags_s,
            )).lower()
            hits = sum(1 for t in kw_terms if t in blob)
            if hits == 0:
                continue
            scored.append((hits, c))
        # Strongest keyword matches first; cap per role.
        scored.sort(key=lambda x: (x[0], x[1].get("team_size") or 0), reverse=True)
        out: list[dict] = []
        for _, c in scored[: self.per_role_limit]:
            name = (c.get("name") or "").strip()
            slug = (c.get("slug") or "").strip()
            yc_url = (c.get("url") or "").strip()
            if slug:
                apply_url = f"https://www.ycombinator.com/companies/{slug}/jobs"
            elif yc_url:
                apply_url = yc_url.rstrip("/") + "/jobs"
            else:
                apply_url = (c.get("website") or "").strip()
            if not apply_url:
                continue
            batch = (c.get("batch") or "").strip()
            tags = c.get("tags") or []
            tags_s = ", ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
            locs = c.get("all_locations") or ""
            if isinstance(locs, list):
                locs = ", ".join(str(x) for x in locs)
            desc = (
                f"{c.get('one_liner') or ''}\n\n{c.get('long_description') or ''}\n\n"
                f"Industry: {c.get('industry') or 'n/a'}"
                f"{' / ' + c.get('subindustry') if c.get('subindustry') else ''} | "
                f"Team size: {c.get('team_size') or 'n/a'} | "
                f"Tags: {tags_s or 'n/a'} | Locations: {locs or 'n/a'}\n"
                f"This Y Combinator company ({batch or 'YC'}) is currently hiring — "
                f"click to view open roles."
            ).strip()
            out.append({
                "id": f"yc:{slug or c.get('id') or name.lower().replace(' ', '-')}",
                "source": "ycombinator",
                "url": apply_url,
                "title": f"{name}{f' (YC {batch})' if batch else ''} — open roles",
                "company": name,
                "location": (locs.split(",")[0].strip() if locs else "See listings"),
                "description": desc[:6000],
                "posted_at": "",  # company-level; not subject to the freshness window
                "yc_batch": batch,
            })
        log.info("ycombinator: %d companies for %r", len(out), keywords)
        return out


def _is_latin_title(title: str) -> bool:
    """Return True if the title is primarily Latin-script (ASCII + accented chars).
    Rejects titles dominated by CJK, Hangul, Arabic, etc."""
    if not title:
        return True
    non_latin = sum(1 for c in title if ord(c) > 0x024F)
    return non_latin / len(title) < 0.25


# ---------- Watched company careers pages ----------

CAREERS_PATHS = [
    "/careers", "/jobs", "/join", "/open-roles", "/open-positions",
    "/openings", "/opportunities", "/positions", "/work-with-us", "/work-here",
    "/about/careers", "/about/jobs", "/company/careers", "/company/jobs",
    "/careers/jobs", "/careers/open-positions", "/en/careers", "/us/careers",
    "/career", "/job-openings", "/hiring",
]

ATS_TEMPLATES = [
    "https://boards.greenhouse.io/{slug}",
    "https://job-boards.greenhouse.io/{slug}",
    "https://boards.eu.greenhouse.io/{slug}",
    "https://jobs.lever.co/{slug}",
    "https://apply.workable.com/{slug}",
    "https://{slug}.bamboohr.com/jobs",
    "https://jobs.ashbyhq.com/{slug}",
    "https://careers.smartrecruiters.com/{slug}",
    "https://{slug}.jobs.jobvite.com/careers",
    "https://{slug}.icims.com/jobs/search",
    "https://{slug}.breezy.hr",
    "https://job.pinpoint.com/{slug}",
]

# ATS hosts where individual job links don't follow the generic /job*/career* pattern.
# For these hosts we trust any link whose path has >= 2 segments (listing page = 1 segment).
_ATS_JOB_HOSTS = frozenset({
    "greenhouse.io", "lever.co", "workable.com", "bamboohr.com", "ashbyhq.com",
    "smartrecruiters.com", "jobvite.com", "icims.com", "breezy.hr", "pinpoint.com",
    "myworkdayjobs.com",
})

_CAREERS_HOST_ALIASES = {
    "dbt.com": {"getdbt.com"},
    "doordash.com": {"careersatdoordash.com"},
    "github.com": {"github.careers"},
    "iextrading.com": {"iex.io"},
    "notion.so": {"notion.com"},
    "spotify.com": {"lifeatspotify.com", "spotifyjobs.com"},
}


class CompanyCareersSource:
    name = "careers"

    def __init__(self, discovery_candidates: int | None = None):
        self.discovery_candidates = (
            discovery_candidates
            if discovery_candidates is not None
            else DEFAULT_SOURCE_LIMITS["watched_company_discovery_candidates"]
        )

    def search_for_company(self, domain: str, company_name: str,
                           keyword_groups: list[str],
                           max_age_hours: int,
                           preferred_url: str = "",
                           time_limit: int = 0) -> tuple[list[dict], bool, str]:
        """Returns (jobs, any_page_reachable, canonical_url).

        The scanner resolves one canonical careers/ATS URL per company. Once a
        URL is cached in companies.careers_url, future runs scan only that URL.
        time_limit: if > 0, stop trying new candidates after this many seconds.
        """
        domain = self._clean_domain(domain)
        if not domain:
            log.info("careers[%s]: skipped (invalid company domain)", company_name or domain)
            return [], False, ""

        candidates = [preferred_url] if preferred_url else self._candidate_urls(domain, company_name)
        if not preferred_url and self.discovery_candidates and self.discovery_candidates > 0:
            candidates = candidates[:self.discovery_candidates]

        deadline = time.monotonic() + time_limit if time_limit > 0 else float("inf")
        first_reachable = ""
        for url in candidates:
            if time.monotonic() > deadline:
                log.warning("careers[%s]: per-company time limit reached, stopping", domain)
                break
            url = self._canonical_url(url)
            if not self._trusted_careers_url(url, domain):
                log.debug("careers[%s]: rejected untrusted candidate %s", domain, url)
                continue

            # JSON API fast-path — Greenhouse and Lever expose free public APIs
            # that return structured job data directly, no HTML parsing needed.
            p_url = urlparse(url)
            host_u = p_url.netloc.lower()
            slug_u = (p_url.path.strip("/").split("/")[0] or "")
            if slug_u and host_u in ("boards.greenhouse.io", "job-boards.greenhouse.io",
                                     "boards.eu.greenhouse.io"):
                result = self._try_greenhouse_api(
                    slug_u, company_name or domain.split(".")[0], keyword_groups)
                if result is not None:
                    first_reachable = first_reachable or url
                    log.info("careers[%s]: greenhouse-api %s (%d matched)",
                             domain, url, len(result))
                    return result, True, url
            elif slug_u and host_u == "jobs.lever.co":
                result = self._try_lever_api(
                    slug_u, company_name or domain.split(".")[0], keyword_groups)
                if result is not None:
                    first_reachable = first_reachable or url
                    log.info("careers[%s]: lever-api %s (%d matched)",
                             domain, url, len(result))
                    return result, True, url

            try:
                html = _http_get(url, timeout=20)
            except Exception:
                try:
                    html = _playwright_get(url, timeout=25)
                except Exception:
                    continue
            first_reachable = first_reachable or url
            # Playwright fallback for JS-rendered pages that return near-empty HTML.
            if len(html) < 800:
                try:
                    html = _playwright_get(url, timeout=25)
                except Exception:
                    pass
            jobs = self._extract_jobs(url, html, domain, company_name)
            if jobs:
                filtered = self._filter(jobs, keyword_groups)
                log.info("careers[%s]: using %s (%d jobs, %d matched)",
                         domain, url, len(jobs), len(filtered))
                return filtered, True, url

        if first_reachable:
            log.info("careers[%s]: using %s (reachable, 0 jobs)", domain, first_reachable)
            return [], True, first_reachable

        log.info("careers[%s]: no careers page reachable", domain)
        return [], False, ""

    def _candidate_urls(self, domain: str, company_name: str) -> list[str]:
        out: list[str] = []
        # Direct company pages first — highest hit rate for well-known companies
        # without slug guessing. /careers and /jobs cover 80%+ of targets.
        for path in CAREERS_PATHS:
            out.append(f"https://{domain}{path}")
        for path in CAREERS_PATHS:
            out.append(f"https://www.{domain}{path}")
        # ATS templates second — two slug variants (hyphen and joined).
        base = (company_name or domain.split(".")[0]).lower()
        slug_hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        slug_clean = re.sub(r"[^a-z0-9]+", "", base)
        slugs_seen: set[str] = set()
        for slug in [slug_hyphen, slug_clean]:
            if slug and slug not in slugs_seen:
                slugs_seen.add(slug)
                for tmpl in ATS_TEMPLATES:
                    out.append(tmpl.format(slug=slug))
        return out

    def _clean_domain(self, domain: str) -> str:
        domain = (domain or "").strip().lower()
        domain = re.sub(r"^https?://", "", domain).split("/", 1)[0].replace("www.", "")
        if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
            return ""
        return domain

    def _canonical_url(self, url: str, keep_query: bool = False) -> str:
        p = urlparse(url or "")
        if not p.scheme or not p.netloc:
            return ""
        host = p.netloc.lower()
        path = re.sub(r"/+", "/", p.path or "/").rstrip("/") or "/"
        query = ""
        if keep_query and p.query:
            kept = [
                (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if not k.lower().startswith("utm_")
                and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
            ]
            query = urlencode(kept)
        return urlunparse(("https", host, path, "", query, ""))

    def _trusted_careers_url(self, url: str, company_domain: str) -> bool:
        if not url:
            return False
        p = urlparse(url)
        host = p.netloc.lower().replace("www.", "")
        path = p.path.lower()
        ats_hosts = {
            "boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
            "jobs.lever.co",
            "apply.workable.com",
            "jobs.ashbyhq.com",
            "careers.smartrecruiters.com",
            "job.pinpoint.com",
        }
        if host in ats_hosts:
            return bool(path.strip("/"))
        if host.endswith(".myworkdayjobs.com"):
            return True
        if host.endswith(".bamboohr.com") and path.startswith("/jobs"):
            return True
        if host.endswith(".jobs.jobvite.com"):
            return True
        if host.endswith(".icims.com"):
            return True
        if host.endswith(".breezy.hr"):
            return True
        for alias in _CAREERS_HOST_ALIASES.get(company_domain, set()):
            if host == alias or host.endswith("." + alias):
                return True
        return host == company_domain or host.endswith("." + company_domain)

    def _extract_jobs(self, page_url: str, html: str, domain: str,
                      company_name: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        out: list[dict] = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True)
            if not text or len(text) < 4:
                continue
            full = self._canonical_url(
                href if href.startswith("http") else urljoin(page_url, href),
                keep_query=True,
            )
            if not full:
                continue
            if not self._is_job_link(href, full) or not self._trusted_job_url(full, page_url, domain):
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append({
                "id": f"careers:{full}",
                "source": "careers",
                "url": full,
                "title": text[:160],
                "company": company_name or domain.split(".")[0],
                "location": "", "description": "", "posted_at": "",
            })
        return out

    def _is_job_link(self, href: str, full_url: str) -> bool:
        """Return True if this link looks like an individual job posting."""
        host = urlparse(full_url).netloc.lower().replace("www.", "")
        path = urlparse(full_url).path
        # For known ATS hosts, trust any link whose path has >= 2 segments.
        for ats in _ATS_JOB_HOSTS:
            if host == ats or host.endswith("." + ats):
                return path.strip("/").count("/") >= 1
        # Keyword-segment match — /jobs/foo, /careers/bar, /positions/baz, etc.
        if re.search(
            r"/(job|jobs|career|careers|position|positions|opening|openings|"
            r"role|roles|listing|posting|vacancy|vacancies|opportunities|"
            r"apply|join|hiring)/",
            href, re.I,
        ):
            return True
        # Numeric/slug ID leaf under a career/job parent — /careers/12345, /jobs/req-abc
        if re.search(r"/(careers?|jobs?|positions?|openings?)/[a-z0-9_%\-]{3,}$",
                     path, re.I):
            return True
        return False

    def _trusted_job_url(self, job_url: str, source_url: str, company_domain: str) -> bool:
        if not job_url:
            return False
        job_host = urlparse(job_url).netloc.lower().replace("www.", "")
        source_host = urlparse(source_url).netloc.lower().replace("www.", "")
        if job_host == source_host:
            return True
        if job_host == company_domain or job_host.endswith("." + company_domain):
            return True
        return any(job_host == h or job_host.endswith("." + h) for h in _ATS_JOB_HOSTS)

    def _try_greenhouse_api(self, slug: str, company_name: str,
                             keyword_groups: list[str]) -> list[dict] | None:
        """Greenhouse public jobs API. Returns None if no board found (404/error)."""
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 404:
                return None
            if not r.ok:
                return None
            raw = (r.json().get("jobs") or [])
        except Exception:
            return None
        out = []
        for j in raw:
            title = (j.get("title") or "").strip()
            job_url = j.get("absolute_url") or ""
            loc = (j.get("location") or {}).get("name") or ""
            desc_html = j.get("content") or ""
            desc = (BeautifulSoup(desc_html, "lxml").get_text(" ", strip=True)[:4000]
                    if desc_html else "")
            out.append({
                "id": f"careers:greenhouse:{slug}:{j.get('id', '')}",
                "source": "careers",
                "url": job_url,
                "title": title,
                "company": company_name,
                "location": loc,
                "description": desc,
                "posted_at": j.get("updated_at") or "",
            })
        return self._filter(out, keyword_groups)

    def _try_lever_api(self, slug: str, company_name: str,
                       keyword_groups: list[str]) -> list[dict] | None:
        """Lever public postings API. Returns None if no board found."""
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=250"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code in (404, 403):
                return None
            if not r.ok:
                return None
            raw = r.json()
            if not isinstance(raw, list):
                return None
        except Exception:
            return None
        out = []
        for j in raw:
            title = (j.get("text") or "").strip()
            job_url = j.get("hostedUrl") or ""
            loc = (j.get("categories") or {}).get("location") or ""
            desc = (j.get("descriptionPlain") or j.get("description") or "").strip()[:4000]
            created_ms = j.get("createdAt") or 0
            posted_iso = (
                datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).isoformat()
                if created_ms else ""
            )
            out.append({
                "id": f"careers:lever:{slug}:{j.get('id', '')}",
                "source": "careers",
                "url": job_url,
                "title": title,
                "company": company_name,
                "location": loc,
                "description": desc,
                "posted_at": posted_iso,
            })
        return self._filter(out, keyword_groups)

    def _filter(self, jobs: list[dict], keyword_groups: list[str]) -> list[dict]:
        # Drop jobs whose title is not primarily Latin script (Korean, Japanese, Chinese, etc.)
        jobs = [j for j in jobs if _is_latin_title(j.get("title", ""))]
        kws = [k.lower() for grp in keyword_groups for k in grp.split() if len(k) > 2]
        if not kws:
            return jobs
        matched = [
            j for j in jobs
            if any(k in f"{j['title']} {j.get('description', '')}".lower() for k in kws)
        ]
        if not matched:
            # Stem fallback: first 5 chars covers "engineer"/"engineering", "analyt"/"analyst", etc.
            stems = [k[:5] for k in kws if len(k) >= 5]
            if stems:
                matched = [j for j in jobs
                           if any(s in j["title"].lower() for s in stems)]
        # Last resort — return all rather than zero from a live careers page.
        return matched if matched else jobs


# ---------- Aggregator ----------

class BoardApiSource:
    """Caller-supplied job-board APIs (JSearch, Adzuna, SerpAPI).

    Built-in HTML scrapers still run. These sources are additive and only
    activate when the user pastes their own key in config.yaml / setup.
    """
    name = "board-api"

    def __init__(self, spec: dict):
        self.spec = spec or {}
        self.kind = str(self.spec.get("type") or "").strip().lower()
        self.name = f"api:{self.kind or 'custom'}"

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        if self.kind == "jsearch":
            return self._jsearch(keywords, location, max_age_hours)
        if self.kind == "adzuna":
            return self._adzuna(keywords, location, max_age_hours)
        if self.kind == "serpapi":
            return self._serpapi(keywords, location, max_age_hours)
        log.warning("unknown scraper API type %r — skipped", self.kind)
        return []

    def _jsearch(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        key = self.spec.get("api_key") or ""
        host = self.spec.get("host") or "jsearch.p.rapidapi.com"
        if not key:
            return []
        if max_age_hours <= 24:
            posted = "today"
        elif max_age_hours <= 72:
            posted = "3days"
        elif max_age_hours <= 168:
            posted = "week"
        else:
            posted = "month"
        query = " ".join(part for part in (keywords, location) if part).strip()
        out: list[dict] = []
        try:
            r = requests.get(
                f"https://{host}/search",
                headers={
                    "X-RapidAPI-Key": key,
                    "X-RapidAPI-Host": host,
                },
                params={
                    "query": query,
                    "page": "1",
                    "num_pages": "1",
                    "date_posted": posted,
                },
                timeout=25,
            )
            r.raise_for_status()
            rows = (r.json() or {}).get("data") or []
        except Exception as e:
            log.warning("jsearch failed: %s", e)
            return []
        for row in rows:
            jid = str(row.get("job_id") or row.get("job_apply_link") or "")
            if not jid:
                continue
            loc_parts = [row.get("job_city"), row.get("job_state"), row.get("job_country")]
            loc = ", ".join(str(p) for p in loc_parts if p)
            posted_at = row.get("job_posted_at_datetime_utc") or ""
            out.append({
                "id": f"jsearch:{jid}",
                "source": "jsearch",
                "url": row.get("job_apply_link") or row.get("job_google_link") or "",
                "title": row.get("job_title") or "",
                "company": row.get("employer_name") or "",
                "location": loc,
                "description": (row.get("job_description") or "")[:8000],
                "posted_at": posted_at,
            })
        log.info("jsearch: %d jobs for %r", len(out), keywords)
        return out

    def _adzuna(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        app_id = self.spec.get("app_id") or ""
        app_key = self.spec.get("app_key") or self.spec.get("api_key") or ""
        country = (self.spec.get("country") or "us").lower()
        loc_l = (location or "").lower()
        if "united kingdom" in loc_l or loc_l in {"uk", "gb", "england"}:
            country = "gb"
        elif "canada" in loc_l:
            country = "ca"
        if not app_id or not app_key:
            return []
        days = max(1, int((max_age_hours or 24) / 24))
        try:
            r = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": keywords,
                    "where": location,
                    "max_days_old": days,
                    "results_per_page": 50,
                    "content-type": "application/json",
                },
                timeout=25,
            )
            r.raise_for_status()
            rows = (r.json() or {}).get("results") or []
        except Exception as e:
            log.warning("adzuna failed: %s", e)
            return []
        out = []
        for row in rows:
            jid = str(row.get("id") or row.get("redirect_url") or "")
            if not jid:
                continue
            loc = ""
            area = row.get("location") or {}
            if isinstance(area, dict):
                loc = ", ".join(area.get("area") or []) or area.get("display_name") or ""
            created = row.get("created") or ""
            company = ""
            if isinstance(row.get("company"), dict):
                company = row["company"].get("display_name") or ""
            out.append({
                "id": f"adzuna:{jid}",
                "source": "adzuna",
                "url": row.get("redirect_url") or "",
                "title": row.get("title") or "",
                "company": company,
                "location": loc,
                "description": (row.get("description") or "")[:8000],
                "posted_at": created,
            })
        log.info("adzuna: %d jobs for %r", len(out), keywords)
        return out

    def _serpapi(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        key = self.spec.get("api_key") or ""
        if not key:
            return []
        chips = "date_posted:today" if max_age_hours <= 24 else (
            "date_posted:week" if max_age_hours <= 168 else "date_posted:month"
        )
        try:
            r = requests.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google_jobs",
                    "q": keywords,
                    "location": location,
                    "api_key": key,
                    "chips": chips,
                },
                timeout=25,
            )
            r.raise_for_status()
            rows = (r.json() or {}).get("jobs_results") or []
        except Exception as e:
            log.warning("serpapi failed: %s", e)
            return []
        out = []
        for row in rows:
            jid = str(row.get("job_id") or row.get("share_link") or row.get("title") or "")
            if not jid:
                continue
            apply = ""
            options = row.get("apply_options") or []
            if options and isinstance(options, list):
                apply = (options[0] or {}).get("link") or ""
            posted = ""
            ext = row.get("detected_extensions") or {}
            if isinstance(ext, dict):
                posted = ext.get("posted_at") or ""
            out.append({
                "id": f"serpapi:{jid}",
                "source": "serpapi",
                "url": apply or row.get("share_link") or "",
                "title": row.get("title") or "",
                "company": row.get("company_name") or "",
                "location": row.get("location") or location,
                "description": (row.get("description") or "")[:8000],
                "posted_at": posted,
            })
        log.info("serpapi: %d jobs for %r", len(out), keywords)
        return out


def _configured_sources(source_limits: dict | None = None,
                        scraper_apis: list | None = None):
    sources = [
        LinkedInSource(
            max_jobs=_limit_int(
                source_limits, "linkedin_max_jobs_per_role",
                DEFAULT_SOURCE_LIMITS["linkedin_max_jobs_per_role"], 1
            )
        ),
        JobrightSource(
            max_pages=_limit_int(
                source_limits, "jobright_max_pages_per_role",
                DEFAULT_SOURCE_LIMITS["jobright_max_pages_per_role"], 1
            )
        ),
        GlassdoorSource(
            max_pages=_limit_int(
                source_limits, "glassdoor_max_pages_per_role",
                DEFAULT_SOURCE_LIMITS["glassdoor_max_pages_per_role"], 1
            )
        ),
        AijobsSource(),
        MljobsSource(),
        YCombinatorSource(
            per_role_limit=_limit_int(
                source_limits, "yc_companies_per_role",
                DEFAULT_SOURCE_LIMITS["yc_companies_per_role"], 0
            )
        ),
    ]
    for spec in scraper_apis or []:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type") or "").lower()
        has_key = bool(spec.get("api_key") or spec.get("app_key"))
        if kind == "adzuna":
            has_key = bool(spec.get("app_id") and (spec.get("app_key") or spec.get("api_key")))
        if kind and has_key:
            sources.append(BoardApiSource(spec))
    return sources


def search_all(roles: list[dict], max_age_hours: int,
               source_limits: dict | None = None,
               drop_undated: bool = False,
               scraper_apis: list | None = None) -> list[dict]:
    by_id: dict[str, dict] = {}
    all_sources = _configured_sources(source_limits, scraper_apis)
    for role in roles:
        kw, loc = role.get("keywords", ""), role.get("location", "")
        kw_terms = [t for t in kw.lower().split() if len(t) > 2]
        for src in all_sources:
            try:
                jobs = src.search(kw, loc, max_age_hours)
            except KeyboardInterrupt:
                log.warning("%s.search interrupted by signal — skipping source for this role",
                            src.name)
                jobs = []
            except Exception as e:
                log.warning("%s.search failed: %s", src.name, e)
                jobs = []
            for j in jobs:
                if j.get("reposted"):
                    log.debug("skipping reposted job: %s @ %s", j.get("title"), j.get("company"))
                    continue
                blob = (j.get("title", "") + " " + j.get("description", "")
                        + " " + j.get("company", "")).lower()
                if kw_terms and not any(t in blob for t in kw_terms):
                    continue
                # Freshness gate.
                pat = j.get("posted_at", "")
                if pat:
                    # Date known → drop if outside the window.
                    if not _within_age(pat, max_age_hours):
                        continue
                elif drop_undated:
                    # STRICT freshness: no post date means we cannot confirm the
                    # job is within the window, so drop it. (Set by behavior flag
                    # strict_freshness. Trades volume for guaranteed-fresh rows.)
                    continue
                by_id.setdefault(j["id"], j)
            time.sleep(1)
    return list(by_id.values())


def check_watched_companies(db, keyword_groups: list[str],
                            max_age_hours: int,
                            source_limits: dict | None = None,
                            min_scan_interval_hours: int = 0) -> list[dict]:
    src = CompanyCareersSource(
        discovery_candidates=_limit_int(
            source_limits, "watched_company_discovery_candidates",
            DEFAULT_SOURCE_LIMITS["watched_company_discovery_candidates"], 1
        )
    )
    # Optional total wall-clock budget so an unattended/chunked run can't spend
    # too long scanning careers pages. 0 = unlimited (manual full scan).
    try:
        scan_budget = float(os.environ.get("WATCHED_SCAN_MAX_SECONDS", "0") or "0")
    except ValueError:
        scan_budget = 0.0
    scan_start = time.monotonic()

    # Rotate coverage: scan least-recently-checked companies first so a budgeted
    # run still covers every company across successive runs.
    rows = sorted(db.list_watched(), key=lambda r: (r["last_checked"] or ""))

    out: list[dict] = []
    skipped_fresh = 0
    for row in rows:
        if scan_budget > 0 and (time.monotonic() - scan_start) >= scan_budget:
            log.info("careers: scan budget %.0fs reached — remaining companies next run",
                     scan_budget)
            break
        domain = row["domain"]
        # Skip companies already scanned within the configured interval.
        if min_scan_interval_hours > 0 and _within_age(row["last_checked"], min_scan_interval_hours):
            skipped_fresh += 1
            continue
        stored_url = row["careers_url"] or ""
        was_unreachable = bool(row["careers_unreachable"])
        # Use the cached URL only if it wasn't flagged broken last run; otherwise
        # force discovery so a broken/missing link gets re-found this run.
        use_preferred = bool(stored_url) and not was_unreachable
        try:
            jobs, reachable, canonical_url = src.search_for_company(
                domain, row["name"] or "", keyword_groups, max_age_hours,
                stored_url if use_preferred else "",
                time_limit=90,
            )
        except Exception as e:
            log.warning("careers scan failed for %s: %s", domain, e)
            jobs, reachable, canonical_url = [], False, ""

        # Cached link broken → re-discover a WORKING link and repair the DB.
        if not reachable and use_preferred:
            log.info("careers[%s]: cached link broken — re-discovering working link", domain)
            try:
                jobs, reachable, canonical_url = src.search_for_company(
                    domain, row["name"] or "", keyword_groups, max_age_hours,
                    "",  # ignore the broken cached URL, force fresh discovery
                    time_limit=90,
                )
            except Exception as e:
                log.warning("careers re-discovery failed for %s: %s", domain, e)

        if not reachable:
            log.info("careers[%s]: unreachable — will retry via discovery next run", domain)
            db.mark_careers_unreachable(domain)
        else:
            db.clear_careers_unreachable(domain)
            if canonical_url and canonical_url != stored_url:
                log.info("careers[%s]: saved working link → %s", domain, canonical_url)
                db.set_careers_url(domain, canonical_url)
        out.extend(jobs)
        db.touch_company(domain)
        time.sleep(1)
    if skipped_fresh:
        log.info("careers: skipped %d companies checked within last %dh",
                 skipped_fresh, min_scan_interval_hours)
    return out
