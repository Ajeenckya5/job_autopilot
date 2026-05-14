"""Job sources. Each source returns a list of normalized job dicts:
    {id, source, url, title, company, location, description, posted_at}
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("autopilot.sources")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

DEFAULT_SOURCE_LIMITS = {
    # LinkedIn's guest endpoint is offset-based and generally tops out around
    # the first 1,000 visible postings for a query.
    "linkedin_max_jobs_per_role": 1000,
    # Indeed's start offset is 10-result based; 100 pages covers ~1,000 jobs.
    "indeed_max_pages_per_role": 100,
    # Jobright exposes total counts through its visitor API. 250 * 20 covers
    # up to 5,000 jobs for a role before the source naturally stops.
    "jobright_max_pages_per_role": 250,
    # Used only when a watched company has no cached canonical careers_url yet.
    "watched_company_discovery_candidates": 8,
}


def _limit_int(limits: dict | None, key: str, default: int, minimum: int = 0) -> int:
    raw = (limits or {}).get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _http_get(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def _playwright_get(url: str, timeout: int = 25) -> str:
    import signal as _signal

    def _alarm_handler(sig, frame):
        raise requests.exceptions.Timeout(
            f"playwright hard timeout after {timeout + 40}s"
        )

    # SIGALRM gives a process-level hard kill so a hung Chromium subprocess
    # can never block the daemon indefinitely.
    old_handler = _signal.signal(_signal.SIGALRM, _alarm_handler)
    _signal.alarm(timeout + 40)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                user_agent=UA,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
    finally:
        _signal.alarm(0)
        _signal.signal(_signal.SIGALRM, old_handler)
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
    """Fetch detail text for thin LinkedIn/Indeed rows.

    Search endpoints for these sites often return title-only rows. Detail
    enrichment is intentionally opt-in from autopilot.py for likely-relevant
    rows so we do not fetch hundreds of full pages unnecessarily.
    """
    desc = job.get("description") or ""
    if len(desc.strip()) >= 300:
        return job
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
    html = _http_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
                     timeout=20)
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(".show-more-less-html__markup")
    text = node.get_text(" ", strip=True) if node else soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


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
    _SLEEP_MIN = 1.5
    _SLEEP_MAX = 3.5
    _MAX_RETRIES = 2

    def __init__(self, max_jobs: int | None = None):
        self.max_jobs = max_jobs or DEFAULT_SOURCE_LIMITS["linkedin_max_jobs_per_role"]

    def _fetch_page(self, url: str) -> str | None:
        """Fetch one LinkedIn page with retry-after / exponential-backoff on 429."""
        import requests as _req
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                r = _req.get(url, headers=HEADERS, timeout=25)
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 0))
                    wait = retry_after if retry_after > 0 else 30 * (2 ** attempt)
                    log.warning("linkedin 429 at %s — waiting %ds (attempt %d/%d)",
                                url, wait, attempt + 1, self._MAX_RETRIES + 1)
                    if attempt == self._MAX_RETRIES:
                        return None
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.text
            except Exception as e:
                log.warning("linkedin fetch failed: %s", e)
                return None
        return None

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        tpr = f"&f_TPR=r{max_age_hours*3600}" if max_age_hours <= 168 else ""
        base = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/"
                f"search?keywords={quote_plus(keywords)}&location={quote_plus(location)}{tpr}")
        out: list[dict] = []
        seen_ids: set[str] = set()
        start = 0
        while len(out) < self.max_jobs:
            url = f"{base}&start={start}"
            html = self._fetch_page(url)
            if html is None:
                log.warning("linkedin: stopping at start=%d (fetch failed)", start)
                break
            soup = BeautifulSoup(html, "lxml")
            page_jobs: list[dict] = []
            for li in soup.select("li"):
                a = li.select_one("a[href*='/jobs/view']")
                title_tag = li.select_one("h3, .base-search-card__title")
                comp_tag = li.select_one("h4, .base-search-card__subtitle")
                loc_tag = li.select_one(".job-search-card__location")
                time_tag = li.select_one("time")
                if not (a and title_tag):
                    continue
                url_raw = a["href"].split("?", 1)[0]
                jid_m = re.search(r"(\d{8,})", url_raw)
                jid = jid_m.group(1) if jid_m else url_raw
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
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
            if not page_jobs:
                break
            out.extend(page_jobs)
            start += self.PAGE_SIZE
            time.sleep(random.uniform(self._SLEEP_MIN, self._SLEEP_MAX))
        log.info("linkedin: %d jobs for %r (%d pages)", len(out), keywords, start // self.PAGE_SIZE)
        return out[:self.max_jobs]


# ---------- Indeed (HTML scrape) ----------

class IndeedSource:
    name = "indeed"
    PAGE_SIZE = 10

    def __init__(self, max_pages: int | None = None):
        self.max_pages = max_pages or DEFAULT_SOURCE_LIMITS["indeed_max_pages_per_role"]

    def search(self, keywords: str, location: str, max_age_hours: int) -> list[dict]:
        fromage = max(1, max_age_hours // 24)
        base = (f"https://www.indeed.com/jobs?q={quote_plus(keywords)}"
                f"&l={quote_plus(location)}&fromage={fromage}")
        out: list[dict] = []
        seen_jks: set[str] = set()
        for page in range(self.max_pages):
            start = page * self.PAGE_SIZE
            url = f"{base}&start={start}"
            try:
                html = _playwright_get(url, timeout=25)
            except Exception as e:
                log.warning("indeed fetch failed at page %d: %s", page, e)
                break
            page_jobs: list[dict] = []
            m = re.search(r'"results"\s*:\s*(\[.*?\])\s*,\s*"sortOptions"', html, flags=re.DOTALL)
            if m:
                try:
                    results = json.loads(m.group(1))
                except Exception:
                    results = []
                for r in results:
                    jk = r.get("jobkey") or r.get("id") or ""
                    if not jk or jk in seen_jks:
                        continue
                    seen_jks.add(jk)
                    company = ""
                    c = r.get("company")
                    if isinstance(c, dict):
                        company = c.get("name", "")
                    elif r.get("companyName"):
                        company = r["companyName"]
                    rel_time = r.get("formattedRelativeTime") or ""
                    page_jobs.append({
                        "id": f"indeed:{jk}",
                        "source": "indeed",
                        "url": f"https://www.indeed.com/viewjob?jk={jk}",
                        "title": r.get("displayTitle") or r.get("title") or "",
                        "company": company,
                        "location": r.get("formattedLocation") or "",
                        "description": (r.get("snippet") or "")[:2000],
                        "posted_at": _relative_to_iso(rel_time),
                        "reposted": bool(r.get("repost")) or "repost" in rel_time.lower(),
                    })
            if not page_jobs:
                soup = BeautifulSoup(html, "lxml")
                for a in soup.select("a[data-jk]"):
                    jk = a["data-jk"]
                    if jk in seen_jks:
                        continue
                    seen_jks.add(jk)
                    page_jobs.append({
                        "id": f"indeed:{jk}",
                        "source": "indeed",
                        "url": f"https://www.indeed.com/viewjob?jk={jk}",
                        "title": a.get_text(strip=True),
                        "company": "", "location": "", "description": "", "posted_at": "",
                    })
            if not page_jobs:
                break
            out.extend(page_jobs)
            time.sleep(2)
        log.info("indeed: %d jobs for %r (%d pages)", len(out), keywords, page + 1)
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
        r = requests.post(url, headers=headers, json=payload, timeout=30)
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
            desc_bits = [
                jr.get("jobNlpTitle") or "",
                jr.get("employmentType") or "",
                jr.get("workModel") or "",
                jr.get("jobSeniority") or "",
                jr.get("jobSummary") or "",
                " ".join(str(x) for x in requirements),
                " ".join(str(x) for x in recommendation_tags),
                f"{min_years}+ years exp" if min_years else "",
            ]
            out.append({
                "id": f"jobright:{jid}",
                "source": "jobright",
                "url": f"https://jobright.ai/jobs/info/{jid}",
                "title": jr.get("jobTitle") or "",
                "company": cr.get("companyName") or "",
                "location": jr.get("jobLocation") or location,
                "description": " ".join(b for b in desc_bits if b),
                "posted_at": posted,
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

# ---------- Watched company careers pages ----------

CAREERS_PATHS = ["/careers", "/jobs"]
ATS_TEMPLATES = [
    "https://boards.greenhouse.io/{slug}",
    "https://jobs.lever.co/{slug}",
    "https://apply.workable.com/{slug}",
    "https://{slug}.bamboohr.com/jobs",
]


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
                           preferred_url: str = "") -> tuple[list[dict], bool, str]:
        """Returns (jobs, any_page_reachable, canonical_url).

        The scanner resolves one canonical careers/ATS URL per company. Once a
        URL is cached in companies.careers_url, future runs scan only that URL.
        """
        domain = self._clean_domain(domain)
        if not domain:
            log.info("careers[%s]: skipped (invalid company domain)", company_name or domain)
            return [], False, ""

        candidates = [preferred_url] if preferred_url else self._candidate_urls(domain, company_name)
        if not preferred_url and self.discovery_candidates and self.discovery_candidates > 0:
            candidates = candidates[:self.discovery_candidates]

        first_reachable = ""
        for url in candidates:
            url = self._canonical_url(url)
            if not self._trusted_careers_url(url, domain):
                log.debug("careers[%s]: rejected untrusted candidate %s", domain, url)
                continue
            try:
                html = _http_get(url, timeout=20)
            except Exception:
                continue
            first_reachable = first_reachable or url
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
        slug = re.sub(r"[^a-z0-9]+", "", (company_name or domain.split(".")[0]).lower())
        for tmpl in ATS_TEMPLATES:
            out.append(tmpl.format(slug=slug))
        for path in CAREERS_PATHS:
            out.append(f"https://{domain}{path}")
        for path in CAREERS_PATHS:
            out.append(f"https://www.{domain}{path}")
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
            "boards.greenhouse.io",
            "jobs.lever.co",
            "apply.workable.com",
        }
        if host in ats_hosts:
            return bool(path.strip("/"))
        if host.endswith(".bamboohr.com") and path.startswith("/jobs"):
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
            if not text or len(text) < 6:
                continue
            if not re.search(r"/(job|jobs|career|careers|position|opening|role|listing|posting)/",
                             href, re.I):
                continue
            full = self._canonical_url(
                href if href.startswith("http") else urljoin(page_url, href),
                keep_query=True,
            )
            if not self._trusted_job_url(full, page_url, domain) or full in seen:
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

    def _trusted_job_url(self, job_url: str, source_url: str, company_domain: str) -> bool:
        if not job_url:
            return False
        job_host = urlparse(job_url).netloc.lower().replace("www.", "")
        source_host = urlparse(source_url).netloc.lower().replace("www.", "")
        if job_host == source_host:
            return True
        if job_host == company_domain or job_host.endswith("." + company_domain):
            return True
        trusted_ats = (
            "greenhouse.io", "lever.co", "workable.com", "bamboohr.com",
        )
        return any(job_host == h or job_host.endswith("." + h) for h in trusted_ats)

    def _filter(self, jobs: list[dict], keyword_groups: list[str]) -> list[dict]:
        kws = [k.lower() for grp in keyword_groups for k in grp.split() if len(k) > 2]
        if not kws:
            return jobs
        return [j for j in jobs
                if any(k in f"{j['title']} {j.get('description','')}".lower() for k in kws)]


# ---------- Aggregator ----------

def _configured_sources(source_limits: dict | None = None):
    return [
        LinkedInSource(
            max_jobs=_limit_int(
                source_limits, "linkedin_max_jobs_per_role",
                DEFAULT_SOURCE_LIMITS["linkedin_max_jobs_per_role"], 1
            )
        ),
        IndeedSource(
            max_pages=_limit_int(
                source_limits, "indeed_max_pages_per_role",
                DEFAULT_SOURCE_LIMITS["indeed_max_pages_per_role"], 1
            )
        ),
        JobrightSource(
            max_pages=_limit_int(
                source_limits, "jobright_max_pages_per_role",
                DEFAULT_SOURCE_LIMITS["jobright_max_pages_per_role"], 1
            )
        ),
    ]


def search_all(roles: list[dict], max_age_hours: int,
               source_limits: dict | None = None) -> list[dict]:
    by_id: dict[str, dict] = {}
    all_sources = _configured_sources(source_limits)
    for role in roles:
        kw, loc = role.get("keywords", ""), role.get("location", "")
        kw_terms = [t for t in kw.lower().split() if len(t) > 2]
        for src in all_sources:
            try:
                jobs = src.search(kw, loc, max_age_hours)
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
                # Drop any job whose date is known and outside the age window
                pat = j.get("posted_at", "")
                if pat and not _within_age(pat, max_age_hours):
                    continue
                by_id.setdefault(j["id"], j)
            time.sleep(1)
    return list(by_id.values())


def check_watched_companies(db, keyword_groups: list[str],
                            max_age_hours: int,
                            source_limits: dict | None = None) -> list[dict]:
    src = CompanyCareersSource(
        discovery_candidates=_limit_int(
            source_limits, "watched_company_discovery_candidates",
            DEFAULT_SOURCE_LIMITS["watched_company_discovery_candidates"], 1
        )
    )
    out: list[dict] = []
    for row in db.list_watched():
        domain = row["domain"]
        if row["careers_unreachable"]:
            log.info("careers[%s]: skipped (careers page was unreachable last check)", domain)
            continue
        try:
            jobs, reachable, canonical_url = src.search_for_company(
                domain, row["name"] or "", keyword_groups, max_age_hours,
                row["careers_url"] or "",
            )
        except Exception as e:
            log.warning("careers scan failed for %s: %s", domain, e)
            jobs, reachable, canonical_url = [], False, ""
        if not reachable:
            log.info("careers[%s]: marking unreachable — will skip next run", domain)
            db.mark_careers_unreachable(domain)
        else:
            db.clear_careers_unreachable(domain)
            if canonical_url and canonical_url != (row["careers_url"] or ""):
                db.set_careers_url(domain, canonical_url)
        out.extend(jobs)
        db.touch_company(domain)
        time.sleep(2)
    return out
