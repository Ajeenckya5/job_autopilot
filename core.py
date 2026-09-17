"""Core modules for job-autopilot.

Optimized to ONE Grok call per job: JD extraction (with web search to find
recruiter + company domain), resume-line content edits, and cold email draft
all come back in a single structured response. Resume tailoring is done by
editing the original PDF in place so the visual format stays identical.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import smtplib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
import yaml

log = logging.getLogger("autopilot")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BLOCK_TITLES = [
    "senior", "sr.", "sr ", " sr", "staff", "principal", "lead ", "head of",
    "director", "manager", "vp ", "vice president", "architect",
    " ii", " iii", " iv", "level 2", "level 3",
]


@dataclass
class Config:
    candidate: dict
    resume_pdf_path: Path
    base_resume: str
    roles: list[dict]
    schedule_minutes: int
    max_per_run: int
    throttle_seconds: int
    output_dir: Path
    xai: dict
    llm_providers: list
    gmail: dict
    mail_tracking: dict
    amf_key: str
    behavior: dict
    filters: dict
    auto_apply: dict
    excel_export_name: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path) as f:
            d = yaml.safe_load(f)
        out_dir = Path(os.path.expanduser(d.get("output_dir", "~/Downloads/job-autopilot")))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "logs").mkdir(exist_ok=True)
        (out_dir / "resumes").mkdir(exist_ok=True)

        pdf_raw = d.get("resume_pdf", "")
        if not pdf_raw:
            raise ValueError("config: 'resume_pdf' must point to your PDF resume")
        pdf_path = Path(os.path.expanduser(pdf_raw)).resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"resume_pdf not found: {pdf_path}")

        base_text = extract_pdf_text(pdf_path)
        if not base_text.strip():
            log.warning("Could not extract text from %s - cold emails may be generic", pdf_path)

        # Default xai.base_url -> xAI; fall back to xAI if absent.
        xai_cfg = dict(d.get("xai") or {})
        xai_cfg.setdefault("base_url", "https://api.x.ai/v1")
        # Optional multi-provider fallback list (overrides xai if non-empty)
        llm_providers_raw = d.get("llm_providers") or []

        filters_cfg = dict(d.get("filters") or {})
        filters_cfg.setdefault("block_title_keywords", DEFAULT_BLOCK_TITLES)
        filters_cfg.setdefault("max_years_required", 3)
        filters_cfg.setdefault("block_phd_required", True)
        filters_cfg.setdefault("block_non_us", True)
        filters_cfg.setdefault("block_no_sponsorship", True)
        filters_cfg.setdefault("block_staffing_agencies", True)
        filters_cfg.setdefault("block_companies", [])

        mail_tracking_cfg = dict(d.get("mail_tracking") or {})
        mail_tracking_cfg.setdefault("enabled", False)
        mail_tracking_cfg.setdefault("provider", "auto")
        mail_tracking_cfg.setdefault("imap_host", "")
        mail_tracking_cfg.setdefault("imap_port", 993)
        mail_tracking_cfg.setdefault("username", "")
        mail_tracking_cfg.setdefault("app_password", "")
        mail_tracking_cfg.setdefault("mailboxes", ["INBOX"])
        mail_tracking_cfg.setdefault("lookback_days", 90)
        mail_tracking_cfg.setdefault("max_messages_per_mailbox", 500)
        mail_tracking_cfg.setdefault("min_match_score", 70)
        mail_tracking_cfg.setdefault("interval_minutes", 60)
        mail_tracking_cfg.setdefault("mark_company_only_if_unique", True)

        return cls(
            candidate=d["candidate"],
            resume_pdf_path=pdf_path,
            base_resume=base_text,
            roles=d["roles"],
            schedule_minutes=int(d.get("schedule_minutes", 360)),
            max_per_run=int(d.get("max_per_run", 10)),
            throttle_seconds=int(d.get("throttle_seconds", 60)),
            output_dir=out_dir,
            xai=xai_cfg,
            llm_providers=llm_providers_raw,
            gmail=d["gmail"],
            mail_tracking=mail_tracking_cfg,
            amf_key=(d.get("anymail_finder", {}) or {}).get("api_key", "") or "",
            behavior=d.get("behavior", {}) or {},
            filters=filters_cfg,
            excel_export_name=d.get("excel_export_name", "") or "",
            auto_apply=d.get("auto_apply", {}) or {},
            raw=d,
        )


def title_is_blocked(title: str, blocklist: list[str]) -> bool:
    """True if the (lowercased) title contains any blocked keyword as a substring."""
    if not title:
        return False
    t = " " + title.lower() + " "
    return any(kw.lower() in t for kw in (blocklist or []))


_TITLE_LEVEL_RE = re.compile(
    r'\b(senior|sr\.?|staff|principal|junior|jr\.?|lead|associate|'
    r'mid[\s\-]?level|entry[\s\-]?level|i{1,3}|iv|vi?)\b',
    re.I,
)


def normalize_title(title: str) -> str:
    """Strip seniority/level markers so 'ML Engineer II' scores like 'ML Engineer'."""
    s = _TITLE_LEVEL_RE.sub(' ', title or '')
    return re.sub(r'\s+', ' ', s).strip().lower()


# Substring keywords that identify staffing/recruiting company names.
# Checked against the lowercased company name (no word-boundary restriction so
# camelCase names like "TechStaffing" are caught too).
_STAFFING_COMPANY_KEYWORDS: tuple[str, ...] = (
    # Core staffing/agency terms
    "staffing", "recruiting", "recruitment",
    # Consulting — compound forms only to avoid blocking Deloitte, BCG, etc.
    "consulting group", "consulting services", "consulting inc", "consulting llc",
    "consulting corp", "consulting co", "consulting firm", "consulting solutions",
    "consultancy",
    # Bare "consulting" only when clearly a small body-shop (no brand ambiguity)
    # — kept as fallback; real FAANG/AI-lab targets never have it in their name.
    "consulting",
    # Talent / workforce intermediaries
    "talent solutions", "talent acquisition", "technical talent",
    "global talent", "workforce solutions", "workforce management",
    "talent bridge", "talent hub", "talent network", "talent pool",
    "talent connect", "talent source", "talent group",
    # Placement / IT body-shops
    "it staffing", "tech staffing",
    "placement services", "placement firm", "placement group",
    # IT services / solutions variants (body-shop signals)
    "it services", "tech services", "technology services",
    "it solutions", "tech solutions",
    # Outsourcing / contract staffing signals
    "outsourcing", "manpower", "contract staffing",
    "subcontract", "bench resources",
    # Headhunting
    "headhunter", "headhunters",
    # Known global staffing / BPO brands
    "insight global", "beaconfire", "beacon fire",
    "tekskills", "tek skills", "lorven",
    "v-soft", "vsoft", "ztekcon",
    "dice",
    "robert half", "kforce", "randstad", "adecco", "kelly services",
    "mastech", "igate", "infosys bpm", "wipro bps", "cognizant bps",
    "syntel", "hexaware", "mphasis bps", "mindtree bps",
    "cybercoders", "teksystems", "tek systems",
    "spherion", "staffmark", "aerotek", "modis",
    "genesis10", "strategic staffing", "apex group",
    "epiq staffing", "vaco", "softpath", "ilink",
    "solugenix", "hirekeyz", "tata consultancy services",
    "infosys staffing", "hcltech", "hcl technologies",
    "ntt data", "dxc technology", "stefanini", "wicresoft",
    "mindteck", "cyient", "niit technologies",
    # More IT services / outsourcing firms
    "capgemini", "epam", "globant", "virtusa", "lancesoft",
    "perficient", "unisys", "kyndryl", "atos ", "atos,",
    "cognizant", "wipro", "infosys",
)

# Staffing signals embedded in the job TITLE — dead giveaways posted by body-shops.
# e.g. "Python Developer (W2 Only)", "ML Engineer | C2C", "Data Eng - Contract"
_STAFFING_TITLE_RE = re.compile(
    r'\bw-?2\b'                                              # W2 / W-2
    r'|\bc-?2-?c\b'                                          # C2C / C-2-C
    r'|\bcorp[\s\-]to[\s\-]corp\b'                           # Corp to Corp
    r'|\bcontract\s+(?:position|role|job|only|to\s+hire)\b'  # "Contract Position/Role"
    r'|\bcontract[\s\-]+(?:engineer|developer|analyst|architect)\b'  # "Contract Engineer"
    r'|(?:^|[\s|(\[{])-\s*contract\b'                        # "… - Contract"
    r'|\bcontract\s*[-|]'                                    # "Contract - …" or "Contract | …"
    r'|\bw-?2\s+only\b'
    r'|\bc-?2-?c\s+(?:only|ok|okay)\b',
    re.IGNORECASE,
)

# Signals in the job description that the poster is a middleman.
# Only patterns essentially NEVER used by real direct employers.
_STAFFING_DESC_RE = re.compile(
    r'(?:'
    r'\bon\s+behalf\s+of\s+(?:our|a)\s+client\b'
    r'|\bour\s+client(?:,|\s+is\s+(?:looking|seeking|searching|hiring|currently)|\s+(?:seeks?|requires?|needs?)\s+(?:a|an)\b)'
    r'|\bour\s+(?:premier|valued|top|key|exclusive)\s+client\b'
    r'|\bhiring\s+for\s+(?:a|our)\s+client\b'
    r'|\bposition\s+(?:is\s+)?with\s+(?:one\s+of\s+)?(?:our|a)\s+clients?\b'
    r'|\bopportunity\s+(?:is\s+)?with\s+(?:one\s+of\s+)?(?:our|a)\s+clients?\b'
    r'|\brole\s+(?:is\s+)?with\s+(?:one\s+of\s+)?(?:our|a)\s+clients?\b'
    r'|\bplaced\s+(?:at|with)\s+(?:our|a)\s+clients?\b'
    r'|\bjoin\s+(?:our|a)\s+client(?:\'s)?\s+(?:team|organization|company|environment)\b'
    r'|\bwork(?:ing)?\s+(?:onsite\s+)?(?:at|for)\s+(?:our|a)\s+client\b'
    r'|\byou\s+will\s+(?:be\s+)?(?:placed|deployed|embedded)\b.{0,60}\b(?:at|with|in)\s+(?:our|a)\s+client\b'
    r'|\bdirect\s+client\s+(?:requirement|position|need|opportunity|opening)\b'
    r'|\bwe\s+are\s+a\s+(?:leading\s+|top\s+|premier\s+)?(?:staffing|consulting|IT\s+consulting|technology\s+consulting)\b'
    r'|\bstaffing\s+(?:agency|firm|company|provider|partner)\b'
    r'|\bconsulting\s+(?:agency|firm|company|provider|services)\b'
    r'|\bIT\s+consulting\s+(?:firm|company|services|provider)\b'
    r'|\btechnology\s+consulting\s+(?:firm|company|services|provider)\b'
    r'|\bthird[\s\-]party\s+(?:contract(?:or)?|staffing|recruiter|placement)\b'
    r'|\bwe\s+(?:place|connect|source)\s+(?:qualified\s+)?candidates\b'
    r'|\bour\s+consultants\s+(?:are\s+placed|work\s+at|are\s+deployed)\b'
    r'|\bmanaged\s+services\s+provider\b'
    r'|\brecruiting\s+(?:agency|firm|company|on\s+behalf)\b'
    r'|\bcontract\s+(?:house|shop)\b'
    r'|\boutside\s+(?:vendor|agency)\b'
    r'|\bvendor\s+(?:to\s+our\s+client|management|bench)\b'
    r'|\bclient\s+site\b.{0,60}\b(?:staffing|consulting|placement)\b'
    r'|\bw-?2\s*[/|]\s*c-?2-?c\b'
    r'|\bc-?2-?c\s*[/|]\s*w-?2\b'
    r'|\bcorp(?:orate)?[\s\-]to[\s\-]corp\b'
    r'|\bsubcontract(?:or|ing|ed)?\b'
    r'|\bvendor\s+bench\b'
    r'|\bbench\s+(?:resources?|candidates?)\b'
    r'|\btalent\s+(?:pool|bench)\s+(?:for|at)\s+(?:our|a)\s+client\b'
    r')',
    re.IGNORECASE,
)


def is_staffing_or_agency(
    company: str, description: str, db: "DB | None" = None, title: str = ""
) -> tuple[bool, str]:
    """Return (True, reason) if the job is from a staffing firm or third-party
    contractor posting on behalf of an actual employer."""
    # Learned user feedback takes highest priority
    if db is not None:
        domain = slug_domain(company or "")
        if domain:
            label = db.get_company_label(domain)
            if label == "staffing":
                return True, f"user-learned: {company!r} flagged as staffing"
            if label == "not_staffing":
                return False, ""
    # Title-embedded staffing signals (W2, C2C, "Contract -" etc.)
    if title:
        m = _STAFFING_TITLE_RE.search(title)
        if m:
            return True, f"staffing/agency filter: title signal {m.group(0)!r}"
    # Static company-name heuristics
    if company:
        cl = company.lower()
        for kw in _STAFFING_COMPANY_KEYWORDS:
            if kw in cl:
                return True, f"staffing/agency filter: company name {company!r}"
    # Static description patterns (only against description, not company name)
    m = _STAFFING_DESC_RE.search(description or "")
    if m:
        return True, f"staffing/agency filter: {m.group(0)!r}"
    # User-kept staffing description patterns (only active=1 rows in DB).
    # Only active when use_learned_patterns: true in config (behavior section).
    if _USE_LEARNED_PATTERNS and db is not None:
        for pat in db.get_learned_patterns("staffing_desc"):
            try:
                if re.search(pat, description or "", re.IGNORECASE):
                    return True, f"staffing filter: {pat!r}"
            except re.error:
                pass
    return False, ""


_VISA_BLOCK_RE = re.compile(
    r'(?:'
    # Security clearance
    r'\bsecurity\s+clearance\b'
    r'|\bclearance\s+required\b'
    r'|\btop[\s\-]*secret\b'
    r'|\bts[/\-]sci\b'
    r'|\bdod\s+(?:secret|clearance)\b'
    r'|\bsecret\s+clearance\b'
    r'|\bactive\s+(?:secret\s+)?clearance\b'
    # No H1B / no visa sponsorship
    r'|\bno\s+(?:visa\s+)?sponsorship\b'
    r'|\b(?:cannot|can\s*not|will\s+not|unable\s+to|does\s+not)\s+sponsor\b'
    r'|\bdoes\s+not\s+(?:offer|provide)\s+(?:visa\s+)?sponsorship\b'
    r'|\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)\b'
    r'|\bnot\s+(?:offering|providing)\s+(?:visa\s+)?sponsorship\b'
    r'|\bnot\s+eligible\s+for\s+(?:visa\s+)?sponsorship\b'
    r'|\bno\s+h[\s\-]?1[\s\-]?b\b'
    r'|\bh[\s\-]?1[\s\-]?b\s+not\s+(?:sponsored|available|supported)\b'
    r'|\bvisa\s+sponsorship\s+(?:is\s+)?not\b'
    r'|\bnot\s+able\s+to\s+(?:provide|offer)\s+(?:visa\s+)?sponsorship\b'
    # "must be eligible to work ... without sponsorship" variants
    r'|\beligib(?:le|ility)\s+to\s+work\b.{0,80}without\s+(?:visa\s+)?sponsorship\b'
    r'|\bauthorized\s+to\s+work\b.{0,80}without\s+(?:visa\s+)?sponsorship\b'
    r'|\bwork\s+without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship\b'
    r'|\bwork\s+authorization\s+without\s+sponsorship\b'
    # Unrestricted work authorization (OPT/F-1 is restricted)
    r'|\bunrestricted\s+(?:us\s+)?work\s+(?:authorization|eligibility|permit)\b'
    r'|\bpermanent\s+(?:us\s+)?work\s+authorization\b'
    # No OPT / CPT
    r'|\bno\s+(?:opt|cpt)\b'
    r'|\bnot\s+(?:available|open)\s+for\s+(?:opt|cpt)\b'
    # US Citizen / GC only
    r'|\b(?:us|u\.s\.)\s+citizens?\s+only\b'
    r'|\bonly\s+(?:us|u\.s\.)\s+citizens?\b'
    r'|\bmust\s+be\s+a?\s*(?:us|u\.s\.)\s+citizen\b'
    r'|\b(?:gc|green\s+card)\s+or\s+(?:us\s+)?citizen\s+(?:only|required)\b'
    r'|\bcitizen\s+or\s+(?:gc|green\s+card)\s+(?:only|required)\b'
    r'|\bcitizenship\s+required\b'
    r'|\bpermanent\s+resident\s+or\s+(?:us\s+)?citizen\s+(?:only|required)\b'
    r'|\bgc\s+or\s+citizen\s+(?:only|required)\b'
    r')',
    re.IGNORECASE,
)


def visa_sponsorship_blocked(title: str, description: str) -> tuple[bool, str]:
    """Return (True, reason) if the job requires security clearance, citizenship-only,
    or explicitly states no H1B/visa sponsorship."""
    text = f"{title or ''} {description or ''}"
    m = _VISA_BLOCK_RE.search(text)
    if m:
        return True, f"visa/clearance filter: {m.group(0)!r}"
    return False, ""


def phd_required_blocked(text: str) -> tuple[bool, str]:
    """Return (True, reason) when the JD explicitly requires a PhD, not merely prefers one."""
    if not text:
        return False, ""
    for m in _PHD_HARD_BLOCK_RE.finditer(text):
        window = text[max(0, m.start() - 40): min(len(text), m.end() + 40)]
        if _PHD_PREFERRED_RE.search(window):
            continue
        return True, f"PhD required filter: {m.group(0)!r}"
    return False, ""


def location_blocked(location: str) -> tuple[bool, str]:
    """Return (True, reason) if the job location is clearly outside the USA."""
    if not location:
        return False, ""
    m = _NON_US_LOCATION_RE.search(location)
    if m:
        return True, f"location filter: non-US location {m.group(0)!r} in {location!r}"
    return False, ""


def configured_max_years(filters: dict) -> int:
    raw = (filters or {}).get("max_years_required", 3)
    m = re.search(r"\d+", str(raw))
    return int(m.group()) if m else 2


_EXP_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15,
}
_EXP_NUM = r"(?:[0-9]|1[0-5])"
_EXP_TAIL = (
    r"(?:\s+(?:of|in|with|as)\s+"
    r"(?:(?:professional|relevant|related|industry|work|hands[\s-]*on|"
    r"software|engineering|technical|machine\s+learning|ml|ai|data\s+science|"
    r"development|production)\s+){0,5}"
    r"(?:experience|exp|software|engineering|development|machine\s+learning|ml|ai|"
    r"data\s+science|analytics|python|java|c\+\+|research))?"
)
_EXP_RANGE_RE = re.compile(
    rf"\b(?P<lo>{_EXP_NUM})\s*(?:-|to|through|–|—)\s*(?P<hi>{_EXP_NUM})\s*\+?\s*"
    rf"(?:years?|yrs?)\b{_EXP_TAIL}",
    re.I,
)
_EXP_PLUS_RE = re.compile(
    rf"\b(?P<num>{_EXP_NUM})\s*(?:\+|plus|or\s+more|and\s+above|or\s+greater)\s*"
    rf"(?:years?|yrs?)\b{_EXP_TAIL}"
    rf"|\b(?P<num_after>{_EXP_NUM})\s*(?:years?|yrs?)\s*"
    rf"(?:\+|plus|or\s+more|and\s+above|or\s+greater)\b{_EXP_TAIL}",
    re.I,
)
_EXP_MIN_RE = re.compile(
    rf"\b(?:minimum|min\.?|at\s+least|requires?|required|must\s+have|need(?:ed|s)?|"
    rf"looking\s+for|over|more\s+than|greater\s+than)\s+(?:of\s+)?"
    rf"(?P<num>{_EXP_NUM})\s*\+?\s*(?:years?|yrs?)\b{_EXP_TAIL}",
    re.I,
)
_EXP_PLAIN_RE = re.compile(
    rf"\b(?P<num>{_EXP_NUM})\s*"
    rf"(?:years?|yrs?)\b\s+(?:of\s+)?"
    rf"(?:(?:professional|relevant|related|industry|work|hands[\s-]*on|software|"
    rf"engineering|technical|machine\s+learning|ml|ai|data\s+science|development|"
    rf"production)\s+){{0,5}}(?:experience|exp)\b",
    re.I,
)
_EXP_MIN_AFTER_RE = re.compile(
    rf"\b(?P<num>{_EXP_NUM})\s*(?:years?|yrs?)\b{_EXP_TAIL}\s+"
    rf"(?:minimum|required|min\.?|or\s+more|or\s+greater)\b",
    re.I,
)
_EXP_CONTEXT_RE = re.compile(
    r"\b(requirements?|qualifications?|minimum|must\s+have|required|"
    r"professional\s+experience|work\s+experience|experience\s+required)\b",
    re.I,
)
_PREFERRED_CONTEXT_RE = re.compile(r"\b(preferred|nice\s+to\s+have|bonus|plus)\b", re.I)


def _normalize_experience_words(text: str) -> str:
    def repl(m: re.Match) -> str:
        return str(_EXP_NUM_WORDS[m.group(0).lower()])
    words = "|".join(re.escape(w) for w in sorted(_EXP_NUM_WORDS, key=len, reverse=True))
    return re.sub(rf"\b(?:{words})\b", repl, text, flags=re.I)


def experience_requirement_blocked(text: str, max_years: int = 2) -> tuple[bool, str]:
    """Block jobs whose required minimum experience is above the configured cap.

    With max_years=3, this blocks "4+ years", "minimum 4 years", "5 years
    experience", "4-6 years", etc. Ranges use the lower bound, so "0-3" and
    "1-3" remain eligible because the required minimum is at or below the cap.
    """
    if not text:
        return False, ""
    s = _normalize_experience_words(re.sub(r"\s+", " ", text))
    checks: list[tuple[re.Pattern, str]] = [
        (_EXP_RANGE_RE, "range"),
        (_EXP_PLUS_RE, "plus"),
        (_EXP_MIN_RE, "minimum"),
        (_EXP_PLAIN_RE, "plain"),
        (_EXP_MIN_AFTER_RE, "minimum_after"),
    ]
    for regex, kind in checks:
        for m in regex.finditer(s):
            before = s[max(0, m.start() - 8):m.start()]
            if kind in {"plain", "minimum_after"} and re.search(r"(?:-|to|through|–|—)\s*$", before, re.I):
                continue
            n = int(
                m.groupdict().get("lo")
                or m.groupdict().get("num")
                or m.groupdict().get("num_after")
                or 0
            )
            window = s[max(0, m.start() - 90): min(len(s), m.end() + 90)]
            if kind != "plus" and _PREFERRED_CONTEXT_RE.search(window) and not _EXP_CONTEXT_RE.search(window):
                continue
            if n > max_years:
                return True, f"experience filter: requires {n}+ years over cap {max_years}"
    return False, ""


_STOPWORDS = {
    "and", "are", "but", "for", "from", "have", "into", "not", "the", "this",
    "that", "with", "you", "your", "will", "our", "their", "they", "who",
    "what", "when", "where", "why", "how", "job", "role", "work", "team",
    "full", "time", "remote", "hybrid", "onsite", "united", "states",
    "ability", "experience", "strong", "knowledge", "understanding", "skills",
    "looking", "seeking", "help", "build", "building", "using", "use",
    "including", "include", "across", "within", "between", "ensure",
    "develop", "developing", "support", "company", "position", "opportunity",
    "engineer", "engineering", "software", "platform", "systems", "remote",
    "onsite", "hybrid", "full-time", "contract", "internship", "level",
}

# Tech terms get 3× weight in the vocab overlap scoring.
_TECH_SKILLS = frozenset({
    # languages / backend
    "python", "sql", "r", "c++", "fastapi", "streamlit", "git",
    # ML / AI core (from Ajeenckya's resume)
    "ai", "ml", "pytorch", "tensorflow", "transformers", "huggingface", "langchain", "langgraph",
    "llm", "llms", "gpt", "bert", "rag", "finetuning", "lora", "qlora",
    "rlhf", "bayesian", "xgboost", "lightgbm", "catboost",
    "sklearn", "scikit", "embeddings", "pgvector", "agentic",
    "reinforcement", "diffusion", "multimodal", "nlp", "mlops",
    "pandas", "numpy", "scipy", "openai", "anthropic", "gemini",
    "mistral", "llama", "vllm", "cuda", "gpu", "tpu",
    # Agentic / LLM-specific terms from resume projects
    "lightrag", "multi-agent", "tool-use", "function-calling",
    "prompt-engineering", "chain-of-thought", "agent-framework",
    "vector-search", "semantic-search", "inference-pipeline",
    "model-serving", "forecasting", "calibration",
    # robotics / autonomous
    "robotics", "robotic", "robot", "ros", "ros2", "autonomous", "autonomy",
    "perception", "controls", "control", "planning", "slam", "lidar",
    # quant / stats (kept for any quant-adjacent ML roles)
    "stochastic", "statistics", "optimization", "time-series",
    # infra / cloud
    "kubernetes", "docker", "redis", "celery",
    "postgres", "postgresql", "mysql",
    "aws", "azure", "s3", "lambda",
    # data / apis
    "tableau", "powerbi", "fastapi",
})

# ── Direct resume-skill matching ─────────────────────────────────────────────
# Weighted skill set extracted from Ajeenckya's resume.
# Used in a regex word-boundary scan against the raw job text so that even
# a title-only listing gets a meaningful direct-hit score.
#
# Tier 1 (weight 5) — LLM / AI differentiators unique to the resume
# Tier 2 (weight 3) — strong ML / cloud / data skills
# Tier 3 (weight 1) — broad software skills
_RESUME_SKILL_WEIGHTS: dict[str, int] = {
    # Tier 1 — LLM / Agentic (exact resume differentiators, highest signal)
    "rag": 5, "llm": 5, "langchain": 5, "agentic": 5, "rlhf": 5,
    "transformers": 5, "pytorch": 5, "qlora": 5, "lora": 5,
    "pgvector": 5, "knowledge graph": 5, "hugging face": 5,
    "huggingface": 5, "fine-tuning": 5, "finetuning": 5,
    "llama": 5, "gpt-4": 5, "generative ai": 5, "llm agent": 5,
    "lightrag": 5, "agentic workflow": 5, "multi-agent": 5,
    "multi agent": 5, "tool use": 4, "function calling": 4,
    "agent framework": 4, "autonomous agent": 4,
    # Modern LLM / RAG frameworks (2025-26)
    "llamaindex": 5, "llama index": 5, "llama-index": 5,
    "dspy": 4, "instructor": 3,
    "crewai": 4, "crew ai": 4, "autogen": 4, "autogpt": 3,
    "langgraph": 4, "langsmith": 3, "langfuse": 3,
    "semantic kernel": 4, "haystack": 3,
    # Modern vector databases
    "chromadb": 4, "chroma": 3, "pinecone": 4, "qdrant": 4,
    "weaviate": 4, "milvus": 4, "faiss": 4,
    # Modern inference / serving
    "vllm": 4, "tgi": 3, "text generation inference": 3,
    "triton": 3, "onnx": 3, "tensorrt": 3, "ollama": 3,
    # LLM providers (2025-26)
    "claude": 3, "gemini": 3, "grok": 2, "mistral": 3,
    # MLOps / experiment tracking
    "mlflow": 3, "wandb": 3, "weights and biases": 3, "neptune": 2,
    "dvc": 2, "bentoml": 3, "ray": 3, "ray serve": 3,
    # Tier 2 — Strong ML/cloud skills from resume
    "tensorflow": 3, "xgboost": 3, "scikit-learn": 3, "sklearn": 3,
    "bayesian": 3, "fastapi": 3, "docker": 3, "kubernetes": 3,
    "aws": 3, "postgresql": 3, "nlp": 3, "embeddings": 3,
    "mlops": 3, "streamlit": 3, "gpt": 3, "bert": 3,
    "machine learning": 3, "deep learning": 3, "data science": 3,
    "reinforcement learning": 3, "openai": 3, "anthropic": 3,
    "vector database": 3, "feature engineering": 3,
    "semantic search": 3, "vector search": 3,
    "quantitative": 3, "quant": 3, "stochastic": 3,
    "optimization": 3, "time series": 3,
    "sentence transformers": 4, "sentence-transformers": 4,
    "spacy": 3, "nltk": 2,
    "predictive maintenance": 2, "forecasting": 2,
    "calibration": 2, "prompt engineering": 2,
    "inference pipeline": 2, "model serving": 2,
    # Tier 3 — Broad signals
    "ai": 2, "ml": 2, "multimodal": 2,
    "python": 1, "sql": 1, "git": 1, "github actions": 1, "ci/cd": 1,
    "agents": 1, "inference": 1, "model deployment": 1, "s3": 1,
    "lambda": 1, "data pipeline": 1, "anomaly detection": 1,
    "pydantic": 1, "redis": 1, "celery": 1, "gradio": 2, "chainlit": 2,
}

# Strong-match denominator: top-8 resume skill weights — harder to saturate than top-4.
_STRONG_SKILL_HIT_TARGET = float(sum(sorted(_RESUME_SKILL_WEIGHTS.values(), reverse=True)[:8]))

# ── Broad JD skill catalog for required-section coverage analysis ─────────────
# Covers skills the user may NOT have so a JD requiring Java/Scala/PhD/etc.
# is correctly penalized even if those skills aren't in _RESUME_SKILL_WEIGHTS.
_JD_SKILL_CATALOG: dict[str, int] = {
    **_RESUME_SKILL_WEIGHTS,
    # Non-resume primary languages / stacks
    "java": 4, "scala": 4, "golang": 3, "go": 2, "rust": 3,
    "c#": 3, "javascript": 2, "typescript": 2, "node": 1, "react": 1,
    # Big-data / data-engineering stack
    "spark": 4, "pyspark": 4, "hadoop": 3, "kafka": 3, "airflow": 3,
    "dbt": 2, "databricks": 3, "snowflake": 3, "redshift": 2, "bigquery": 2,
    "hive": 2, "flink": 3,
    # Other ML frameworks user doesn't have
    "matlab": 2, "julia": 2, "jax": 3,
    # Vision / speech / specialised ML domains
    "computer vision": 4, "object detection": 3, "segmentation": 2,
    "speech recognition": 3, "asr": 3, "tts": 2,
    "diffusion models": 3, "stable diffusion": 3, "gans": 3,
    "causal inference": 3, "econometrics": 3,
    "recommendation systems": 4, "recommender systems": 4, "collaborative filtering": 3,
    # Production infra not on resume
    "distributed training": 4, "production ml": 3, "large scale": 2,
    "terraform": 2, "helm": 2, "gcp": 2, "azure": 2,
    "a/b testing": 2, "experimentation": 2,
    # Research / academic signals
    "phd": 5, "ph.d": 5, "doctorate": 5, "doctoral": 5,
    "master": 2, "masters": 2, "publications": 4, "peer-reviewed": 4,
    "first-author": 4,
}

# Hard-knockout: primary languages that signal a completely different tech stack.
_STACK_MISMATCH_LANGS = frozenset({
    "java", "scala", "golang", "go", "rust", "c#", "spark", "pyspark",
    "hadoop", "databricks",
})

# Subset used when scanning the FULL description (no explicit required-section
# header). Excludes short/common words ("go", "spark") and platform names
# ("databricks") that appear innocuously in unstructured prose.
_STACK_MISMATCH_LANGS_STRICT = frozenset({
    "java", "scala", "golang", "rust", "c#", "pyspark", "hadoop",
})

# Patterns for academic / experience hard knockouts (applied to required section only)
_PHD_REQ_RE = re.compile(r'\b(ph\.?d\.?|doctorate|doctoral)\b', re.I)
# Hard pre-filter: PhD explicitly required (not just preferred)
_PHD_HARD_BLOCK_RE = re.compile(
    r"\b(?:require[sd]?|must\s+have|mandatory|essential)\b[^.\n]{0,80}?\b(?:ph\.?d\.?|doctorate|doctoral)\b"
    r"|\b(?:ph\.?d\.?|doctorate|doctoral)\b[^.\n]{0,80}?\b(?:required|mandatory|is\s+required)\b",
    re.I,
)
_PHD_PREFERRED_RE = re.compile(
    r'\b(?:preferred?|nice\s+to\s+have|bonus|a\s+plus|plus|ideal|desired|advantageous)\b', re.I
)
# Non-US country / province / city indicators for location pre-filter
_NON_US_LOCATION_RE = re.compile(
    r'\b(?:'
    r'canada|united\s+kingdom|u\.k\.?|uk|great\s+britain|england|scotland|wales|'
    r'ireland|northern\s+ireland|australia|new\s+zealand|india|germany|france|'
    r'netherlands|sweden|denmark|norway|finland|switzerland|austria|belgium|'
    r'spain|italy|portugal|poland|singapore|japan|south\s+korea|china|taiwan|'
    r'mexico|brazil|argentina|pakistan|philippines|'
    # Canadian provinces
    r'ontario|british\s+columbia|alberta|qu[e\xe9]bec|manitoba|saskatchewan|'
    r'nova\s+scotia|new\s+brunswick|newfoundland|prince\s+edward\s+island|'
    # Major non-US cities (unambiguous)
    r'bangalore|bengaluru|hyderabad|mumbai|chennai|pune|kolkata|new\s+delhi|'
    r'toronto|vancouver|calgary|montreal|ottawa|winnipeg|edmonton|'
    r'manchester|birmingham|glasgow|edinburgh|sydney|melbourne|auckland|'
    r'berlin|munich|hamburg|paris|amsterdam|stockholm'
    r')\b',
    re.I,
)
_MASTERS_REQ_RE = re.compile(
    r"\b(master['’]?s?|m\.?s\.?|m\.eng\.?)\b.{0,120}"
    r"\b(required|must|necessary|minimum|preferred)\b", re.I
)
_EXP_YEARS_RE = re.compile(
    r'\b(\d+)\+?\s*(?:[–\-]\s*\d+\s*)?(?:years?|yrs?)\b.{0,80}?\bexperience\b',
    re.I,
)

_NEW_GRAD_RE = re.compile(
    r'\b(new\s+grad(?:uate)?|entry[\s-]level|0[\s\-]?[\-–][\s\-]?[12]\s*(?:years?|yrs?)|'
    r'recent\s+grad(?:uate)?|junior|early[\s-]career|fresh\s+out\s+of|'
    r'new\s+to\s+the\s+(?:field|industry)|associate[\s-]level|'
    r'no\s+(?:prior\s+)?experience\s+required)\b', re.I
)
_H1B_POS_RE = re.compile(
    r'\b(?:'
    r'h[\s\-]?1[\s\-]?b\s+(?:visa\s+)?sponsor(?:ship)?'
    r'|(?:we\s+)?(?:do|will|can|are\s+(?:able|willing)\s+to)\s+(?:offer\s+)?sponsor'
    r'|willing\s+to\s+sponsor'
    r'|sponsorship\s+(?:is\s+)?(?:available|provided|offered|considered)'
    r'|visa\s+sponsorship\s+(?:provided|available|offered|considered)'
    r'|we\s+(?:offer|provide|support)\s+(?:visa\s+)?sponsorship'
    r'|open\s+to\s+(?:opt|cpt|f[\s\-]?1|j[\s\-]?1)\b'
    r'|(?:opt|cpt)\s+(?:students?\s+)?(?:welcome|eligible|accepted|ok)'
    r'|f[\s\-]?1\s+(?:opt\s+)?sponsorship'
    r')\b', re.I
)

# ML/AI presence check — if NONE of these appear in a job, it is probably
# unrelated to your background and gets a score penalty.
_ML_PRESENCE_TERMS = re.compile(
    r"\b(machine learning|deep learning|artificial intelligence|"
    r"llm|llms|nlp|rag|ai|ml|ai/ml|ml/ai|neural|generative|data science|data scientist|"
    r"computer vision|reinforcement|foundation model|large language|"
    r"predictive model|model deployment|forecasting|inference|mlops|"
    r"agentic|agent|multi.agent|langchain|lightrag|transformers|"
    r"robotics|robotic|autonomous systems|perception|controls|"
    r"quant|quantitative|algorithmic trading|alpha research|"
    r"optimization)\b",
    re.I,
)

_NON_TECH_ROLE_RE = re.compile(
    r"\b(content|writer|copywriter|marketing|sales|business analyst|"
    r"operations specialist|customer support|account executive|"
    r"product owner|program manager|project manager)\b",
    re.I,
)

_LLM_MATCH_SYSTEM = """Score how well a candidate matches a job's requirements. Focus ONLY on the required qualifications and core responsibilities.

The candidate's real background is given under "CANDIDATE SKILLS" in the user message — treat THAT as the source of truth. Do NOT assume the candidate is in any particular field; infer their field from the CANDIDATE SKILLS provided. Reward direct overlap between the candidate's actual skills/experience and what the role requires; the role's own domain (software, ML, industrial/manufacturing, operations, data, etc.) is fine as long as it matches the candidate's background.

Score 0-100 (85+=strong match, 60-84=good, 40-59=partial, <40=weak).
Caps: PhD strictly required and candidate lacks it→max 30; role requires substantially more years of experience than the candidate has→max 45; role is in a clearly different field than the candidate's background→max 30.

Return ONLY JSON: {"score": <int>, "fit_summary": "<1 sentence: what the role needs and how the candidate fits>"}"""

_ROLE_SPECIFIC_TERMS = {
    "ai", "ml", "llm", "llms", "rag", "nlp", "agentic", "generative",
    "machine", "learning", "data", "scientist", "science", "applied",
    "research", "model", "models", "vision", "inference", "deployment",
    "deep", "nlp", "multimodal", "mlops", "platform", "infrastructure",
    "agent", "agents", "workflow", "orchestration", "finetuning", "lora",
    "robotics", "robotic", "robot", "autonomous", "autonomy", "perception",
    "quant", "quantitative", "analyst", "finance", "trading",
    "alpha", "portfolio", "stochastic", "optimization", "forecasting", "analytics",
}

_TOKEN_ALIASES = {
    "ml": ("machine", "learning", "machine_learning"),
    "ai": ("artificial", "intelligence"),
    "genai": ("generative", "ai"),
    "llms": ("llm",),
    "nlp": ("natural", "language", "natural_language"),
    "robotic": ("robotics", "robot"),
    "quant": ("quantitative",),
    "quantitative": ("quant",),
}

_ATS_CONCEPT_GRAPH: dict[str, set[str]] = {
    "ai_core": {
        "ai", "artificial", "intelligence", "machine", "learning",
        "machine_learning", "ml", "deep", "neural", "model", "models",
        "predictive", "classification", "regression", "forecasting",
        "calibration", "anomaly", "detection",
    },
    "llm_generative": {
        "llm", "llms", "gpt", "llama", "mistral", "generative", "rag",
        "transformers", "embeddings", "vector", "pgvector", "langchain",
        "langgraph", "agentic", "agents", "fine-tuning", "finetuning",
        "lora", "qlora", "prompt", "retrieval", "lightrag",
        "knowledge_graph", "semantic", "search", "tool_use",
        "function_calling", "multi_agent", "reasoning", "chain_of_thought",
    },
    "agentic_systems": {
        "agent", "agents", "agentic", "multi-agent", "multi_agent",
        "autonomous", "tool_use", "function_calling", "workflow",
        "planning", "react", "chain_of_thought", "memory", "reflection",
        "langchain", "langgraph", "lightrag", "agentbench",
        "orchestration", "execution", "tool", "tools",
    },
    "ml_modeling": {
        "pytorch", "tensorflow", "sklearn", "scikit", "xgboost",
        "lightgbm", "catboost", "bayesian", "reinforcement", "nlp",
        "multimodal", "diffusion", "feature", "features", "training",
        "inference", "evaluation", "deployment", "rlhf", "dpo",
        "fine-tuning", "finetuning", "lora", "qlora", "huggingface",
    },
    "data_science": {
        "data", "scientist", "science", "analytics", "pandas", "numpy",
        "scipy", "sql", "statistics", "statistical", "experimentation",
        "ab", "dashboard", "tableau", "powerbi", "pipeline", "etl",
        "forecasting", "time_series",
    },
    "robotics_ai": {
        "robotics", "robotic", "robot", "ros", "ros2", "autonomous",
        "autonomy", "perception", "controls", "control", "planning",
        "slam", "lidar", "computer", "vision", "navigation", "sensor",
    },
    "engineering_platform": {
        "python", "fastapi", "docker", "kubernetes", "aws", "azure",
        "postgres", "postgresql", "redis", "celery", "api", "apis",
        "backend", "distributed", "cloud", "mlops", "ci/cd", "s3",
        "lambda", "gpu", "cuda", "vllm", "inference_pipeline",
        "model_serving", "model_deployment",
    },
}

_CONCEPT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "genai": ("generative", "ai"),
    "retrieval augmented generation": ("rag", "retrieval", "augmented"),
    "large language model": ("llm",),
    "large language models": ("llms", "llm"),
    "natural language processing": ("nlp",),
    "computer vision": ("vision", "perception"),
    "autonomous vehicles": ("autonomous", "robotics"),
    "time series": ("time-series", "timeseries"),
    # Agentic / LLM patterns from resume
    "agentic workflow": ("agentic", "workflow", "agents"),
    "multi-agent": ("agentic", "multi_agent", "agents"),
    "multi agent": ("agentic", "multi_agent", "agents"),
    "tool use": ("tool_use", "function_calling", "agents"),
    "function calling": ("tool_use", "function_calling"),
    "knowledge graph rag": ("rag", "knowledge_graph", "retrieval"),
    "lightrag": ("rag", "knowledge_graph", "retrieval"),
    "chain of thought": ("chain_of_thought", "reasoning"),
    "prompt engineering": ("prompt", "engineering"),
    "model fine-tuning": ("finetuning", "fine-tuning", "lora"),
    "model finetuning": ("finetuning", "fine-tuning", "lora"),
    "inference pipeline": ("inference", "inference_pipeline", "serving"),
    "model serving": ("inference", "model_serving", "deployment"),
    "predictive maintenance": ("predictive", "maintenance", "anomaly"),
    "anomaly detection": ("anomaly", "detection", "predictive"),
    "feature engineering": ("feature", "engineering", "features"),
}


_REQUIRED_SECTION_RE = re.compile(
    r"(?:required\s+qualifications?|minimum\s+qualifications?|"
    r"must\s+have|requirements?:|you\s+(?:must|will)\s+have|"
    r"what\s+you[''']ll\s+bring|basic\s+qualifications?|"
    r"what\s+we(?:[''']re)?\s+looking\s+for|"
    r"you\s+should\s+have|you\s+(?:need|have):|"
    r"required\s+skills?|technical\s+requirements?|"
    r"qualifications?\s*(?:required|needed)|we\s+require|"
    r"mandatory\s+(?:skills?|qualifications?)|"
    r"core\s+(?:skills?|requirements?|qualifications?)|"
    r"key\s+(?:skills?|requirements?)|"
    r"skills?\s+(?:required|needed|we(?:[''']re)?\s+looking\s+for))",
    re.I,
)
_PREFERRED_SECTION_RE = re.compile(
    r"(?:preferred\s+qualifications?|nice\s+to\s+have|bonus|plus\s+if|"
    r"ideal\s+candidate|preferred\s+skills?|good\s+to\s+have)",
    re.I,
)


def _split_required_preferred(desc: str) -> tuple[str, str]:
    """Split JD description into (required_section, preferred_section).

    Real ATS systems (Workday, Greenhouse, Lever) weight required-section skill
    matches ~2-3× more than preferred-section matches when screening resumes.
    This mirrors that behavior by scoring them separately.
    """
    req_m = _REQUIRED_SECTION_RE.search(desc)
    pref_m = _PREFERRED_SECTION_RE.search(desc)

    if not req_m and not pref_m:
        return desc, ""

    if req_m and not pref_m:
        return desc[req_m.start():], ""

    if pref_m and not req_m:
        return desc[:pref_m.start()], desc[pref_m.start():]

    if req_m.start() < pref_m.start():
        return desc[req_m.start():pref_m.start()], desc[pref_m.start():]
    else:
        return desc[req_m.start():], desc[pref_m.start():req_m.start()]


def _skill_hit_score(title: str, desc: str) -> float:
    """0.0–1.0: fraction of resume skills (by weight) found in the job.

    Required-section hits count 2× vs preferred/general hits to mirror
    how real ATS parsers weight mandatory vs optional qualifications.
    """
    title_l = title.lower()
    req_text, pref_text = _split_required_preferred(desc.lower())
    # Everything not in req or pref sections is general
    general_text = desc.lower() if not req_text else ""

    hits = 0.0
    for skill, w in _RESUME_SKILL_WEIGHTS.items():
        pat = r"\b" + re.escape(skill) + r"\b"
        if re.search(pat, title_l):
            hits += w * 2.0          # title hit = highest signal
        elif req_text and re.search(pat, req_text):
            hits += w * 1.6          # required section = strong signal
        elif pref_text and re.search(pat, pref_text):
            hits += w * 1.0          # preferred = baseline
        elif general_text and re.search(pat, general_text):
            hits += w * 1.0
        elif re.search(pat, desc.lower()):
            hits += w * 0.8          # mentioned somewhere in the full text
    return min(1.0, hits / max(1.0, _STRONG_SKILL_HIT_TARGET))


def _tokenize(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z][a-z0-9+#.\-]{1,}", (text or "").lower())
        if t not in _STOPWORDS and not t.isdigit()
    ]


def _weighted_vocab(text: str) -> dict[str, float]:
    """Return {token: weight} where tech terms weigh 3×, bigrams weigh 2×."""
    tokens = _tokenize(text)
    vocab: dict[str, float] = {}
    for t in tokens:
        w = 3.0 if t in _TECH_SKILLS else 1.0
        if w > vocab.get(t, 0):
            vocab[t] = w
        for alias in _TOKEN_ALIASES.get(t, ()):
            vocab[alias] = max(vocab.get(alias, 0), w)
    for i in range(len(tokens) - 1):
        bg = f"{tokens[i]}_{tokens[i+1]}"
        vocab[bg] = max(vocab.get(bg, 0), 2.0)
    return vocab


def _expanded_text(text: str) -> str:
    low = (text or "").lower()
    additions: list[str] = []
    for phrase, aliases in _CONCEPT_SYNONYMS.items():
        if phrase in low:
            additions.extend(aliases)
    return " ".join([text or "", *additions])


def _concept_vector(text: str) -> dict[str, float]:
    vocab = _weighted_vocab(_expanded_text(text))
    vector: dict[str, float] = {}
    for concept, terms in _ATS_CONCEPT_GRAPH.items():
        score = 0.0
        for term in terms:
            if term in vocab:
                score += vocab[term]
        vector[concept] = score
    return vector


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in set(a) | set(b))
    norm_a = sum(v * v for v in a.values()) ** 0.5
    norm_b = sum(v * v for v in b.values()) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return min(1.0, dot / (norm_a * norm_b))


def _semantic_vector_component(resume_text: str, roles: list[dict], job_text: str) -> float:
    candidate_profile = " ".join([
        resume_text or "",
        " ".join(r.get("keywords", "") for r in roles),
    ])
    return _cosine(_concept_vector(candidate_profile), _concept_vector(job_text))


def _ontology_component(roles: list[dict], job_text: str) -> float:
    job_vec = _concept_vector(job_text)
    best = 0.0
    for role in roles:
        role_vec = _concept_vector(role.get("keywords", ""))
        wanted = {concept for concept, score in role_vec.items() if score > 0}
        if not wanted:
            continue
        hit = sum(1 for concept in wanted if job_vec.get(concept, 0) > 0)
        best = max(best, hit / len(wanted))
    return best


def _best_role_component(roles: list[dict], job_text: str, title: str) -> float:
    """Return the best configured-role match, instead of diluting against all roles."""
    job_vocab = _weighted_vocab(job_text)
    if not job_vocab:
        return 0.0

    best = 0.0
    title_l = title.lower()
    title_norm = normalize_title(title)
    for role in roles:
        role_kw = (role.get("keywords") or "").strip()
        role_l = role_kw.lower()
        if not role_kw:
            continue
        role_vocab = _weighted_vocab(role_kw)
        if not role_vocab:
            continue
        role_possible = sum(role_vocab.values())
        role_hit = sum(w for t, w in role_vocab.items() if t in job_vocab)
        component = role_hit / max(1.0, role_possible)
        role_specific = set(_tokenize(role_kw)) & _ROLE_SPECIFIC_TERMS
        job_specific = set(_tokenize(job_text)) & role_specific
        if role_specific and not job_specific:
            component *= 0.35
        if "engineer" in role_l and not re.search(r"\b(engineer|developer|development)\b", title_l):
            component *= 0.25
        if "scientist" in role_l and not re.search(r"\b(scientist|researcher|research)\b", title_l):
            component *= 0.25
        # Title match: try both original and normalized (strips "II", "Senior", etc.)
        if role_kw.lower() in title_l or role_kw.lower() in title_norm:
            component = max(component, 1.0)
        best = max(best, component)
    return min(1.0, best)


def _top_matching_skills(title: str, desc: str) -> list[str]:
    """Return up to 5 resume skills that appear in the job text, by weight desc."""
    combined = (title + " " + desc).lower()
    hits: list[tuple[int, str]] = []
    for skill, w in _RESUME_SKILL_WEIGHTS.items():
        pat = r"\b" + re.escape(skill) + r"\b"
        if re.search(pat, combined):
            hits.append((w, skill))
    hits.sort(reverse=True)
    return [s for _, s in hits[:5]]


def _build_fallback_fit_summary(breakdown: dict, job: dict) -> str:
    """Generate a human-readable fit summary from keyword scoring data."""
    score = breakdown.get("score", 0)
    caps = breakdown.get("caps") or []
    coverage = breakdown.get("required_section_coverage")
    missing = (breakdown.get("missing_required_skills") or [])[:3]
    title = (job.get("title") or "role").strip()
    top_skills = _top_matching_skills(job.get("title", ""), job.get("description", ""))

    if score >= 75:
        verdict = "Strong match"
    elif score >= 55:
        verdict = "Good match"
    elif score >= 40:
        verdict = "Partial match"
    else:
        verdict = "Weak match"

    parts: list[str] = [f"{title} — {verdict}"]
    if top_skills:
        parts.append(f"matching: {', '.join(top_skills[:4])}")
    if coverage is not None:
        cov_str = f"{coverage:.0f}% req. skills"
        if missing:
            cov_str += f" (gaps: {', '.join(missing[:2])})"
        parts.append(cov_str)
    if caps:
        cap_short = caps[0].split("—")[0].strip().lower()
        parts.append(cap_short)
    return "; ".join(parts) + "."


def _resume_signal_component(resume_text: str, job_text: str) -> float:
    """Resume overlap using only technical terms, so generic words do not inflate scores."""
    job_vocab = _weighted_vocab(job_text)
    resume_vocab = _weighted_vocab(resume_text)
    signal_vocab = {
        term: weight
        for term, weight in job_vocab.items()
        if term in _TECH_SKILLS
        or (
            "_" in term
            and any(part in _TECH_SKILLS or part in _ROLE_SPECIFIC_TERMS for part in term.split("_"))
        )
    }
    if not signal_vocab:
        return 0.0
    possible = sum(signal_vocab.values())
    hit = sum(w for term, w in signal_vocab.items() if term in resume_vocab)
    return min(1.0, hit / max(1.0, possible))


def resume_relevance_score(resume_text: str, roles: list[dict], job: dict) -> int:
    return int(resume_relevance_breakdown(resume_text, roles, job)["score"])


def llm_score_resume_match(
    grok: "Grok",
    resume_text: str,
    roles: list[dict],
    job: dict,
) -> dict | None:
    """LLM resume-JD match focused on key requirements only.

    Prefers the qualifications/responsibilities section of the JD over the full
    description. The resume is sent in full: at ~4-6k chars it is a rounding
    error against these models' context windows, and clipping it hid most of the
    candidate's experience from the scorer.
    Returns None on failure.
    """
    title = (job.get("title") or "").strip()
    desc  = (job.get("description") or "").strip()
    if not desc or len(desc) < 150:
        return None

    # Prefer the requirements section, but keep enough of it to judge fit.
    req_section, _ = _split_required_preferred(desc)
    jd_text = req_section.strip() if len(req_section.strip()) >= 200 else desc
    jd_text = jd_text[:6000]

    user_msg = (
        f"JOB TITLE: {title}\n\n"
        f"KEY REQUIREMENTS:\n{jd_text}\n\n"
        f"CANDIDATE RESUME (full):\n{resume_text[:20000]}"
    )
    try:
        result = grok.chat_json(_LLM_MATCH_SYSTEM, user_msg, timeout=60)
        score = int(min(100, max(0, int(result.get("score", 0) or 0))))
        return {
            "score": score,
            "fit_summary": str(result.get("fit_summary", ""))[:400],
            "match_reasons": [],
            "gaps": [],
            "role_type": "",
            "seniority_fit": "",
        }
    except Exception as exc:
        log.warning("LLM match scoring failed for %r: %s", title, exc)
        return None


# Module-level learned adjustment lists — reloaded by load_learned_score_patterns().
# Kept as module globals so resume_relevance_breakdown() stays a pure function.
_LEARNED_SCORE_BOOSTS: list[str] = []
_LEARNED_SCORE_PENALTIES: list[str] = []
_USE_LEARNED_PATTERNS: bool = True


def set_use_learned_patterns(enabled: bool) -> None:
    """Enable or disable learned score/staffing patterns. Call before scoring."""
    global _USE_LEARNED_PATTERNS
    _USE_LEARNED_PATTERNS = enabled


def load_learned_score_patterns(db: "DB") -> None:
    """Reload score boost/penalty patterns from DB into module-level caches.
    Call once after DB is opened (autopilot.py _bootstrap_state).
    Patterns are only applied when _USE_LEARNED_PATTERNS is True."""
    global _LEARNED_SCORE_BOOSTS, _LEARNED_SCORE_PENALTIES
    _LEARNED_SCORE_BOOSTS    = db.get_learned_patterns("score_boost")
    _LEARNED_SCORE_PENALTIES = db.get_learned_patterns("score_penalty")


def resume_relevance_breakdown(resume_text: str, roles: list[dict], job: dict) -> dict:
    """Transparent ATS-style score.

    This is not Workday or Greenhouse's proprietary algorithm. It mirrors common
    ATS screening behavior: structured job-scorecard calibration, skills-cloud
    ontology matching, synonym expansion, concept-vector similarity, and tiers.
    """
    title = (job.get("title") or "").strip()
    desc  = (job.get("description") or "").strip()
    job_text = f"{title} {desc}"
    if not job_text.strip():
        return {
            "score": 0,
            "scorecard_points": 0,
            "resume_skill_points": 0,
            "semantic_points": 0,
            "ontology_points": 0,
            "technical_overlap_points": 0,
            "title_bonus_points": 0,
            "required_section_coverage": None,
            "missing_required_skills": [],
            "required_years": None,
            "phd_required": False,
            "stack_mismatch": [],
            "tier": "reject",
            "caps": ["empty job text"],
        }

    skill_score = _skill_hit_score(title, desc)
    role_component = _best_role_component(roles, job_text, title)
    resume_component = _resume_signal_component(resume_text, job_text)
    semantic_component = _semantic_vector_component(resume_text, roles, job_text)
    ontology_component = _ontology_component(roles, job_text)
    ml_intent = bool(_ML_PRESENCE_TERMS.search(job_text))

    title_lower = title.lower()
    title_component = 1.0 if any(
        r.get("keywords", "").lower() in title_lower for r in roles
    ) else 0.0

    # ── Required-section analysis ─────────────────────────────────────────────
    # Only runs when the JD has an explicit required/minimum qualifications header.
    # Without the guard, _split_required_preferred returns the full desc as req_section,
    # which would incorrectly trigger the cap on every unstructured JD.
    has_req_section = bool(_REQUIRED_SECTION_RE.search(desc))
    req_section, _ = _split_required_preferred(desc)
    req_skill_hit = 0.0
    req_max = 0.0
    req_hits = 0.0
    missing_required: list[str] = []   # sorted by weight desc (most critical gaps first)
    if has_req_section and req_section:
        req_section_l = req_section.lower()
        resume_l = resume_text.lower()
        _missing_weighted: list[tuple[int, str]] = []
        for skill, w in _JD_SKILL_CATALOG.items():
            pat = r"\b" + re.escape(skill) + r"\b"
            if re.search(pat, req_section_l):
                req_max += w
                if re.search(pat, resume_l):
                    req_hits += w
                else:
                    _missing_weighted.append((w, skill))
        if req_max > 0:
            req_skill_hit = req_hits / req_max
        missing_required = [s for _, s in sorted(_missing_weighted, reverse=True)]

    # ── Hard-knockout signals ─────────────────────────────────────────────────
    phd_required = bool(_PHD_REQ_RE.search(desc))   # scan full JD, not just req section
    resume_has_phd = bool(_PHD_REQ_RE.search(resume_text or ""))

    req_years = 0
    if has_req_section and req_section:
        year_hits = _EXP_YEARS_RE.findall(req_section)
        if year_hits:
            req_years = max(int(m) for m in year_hits)

    stack_mismatch_langs: list[str] = []
    # Use the full language set against the required section (structured JDs);
    # fall back to the strict subset against the full description (unstructured JDs)
    # to avoid false positives from short/common words like "go" and "spark".
    if has_req_section and req_section:
        _mismatch_scan_text = req_section.lower()
        _mismatch_set = _STACK_MISMATCH_LANGS
    else:
        _mismatch_scan_text = desc.lower()
        _mismatch_set = _STACK_MISMATCH_LANGS_STRICT
    resume_l2 = resume_text.lower()
    for lang in _mismatch_set:
        pat = r"\b" + re.escape(lang) + r"\b"
        if re.search(pat, _mismatch_scan_text) and not re.search(pat, resume_l2):
            stack_mismatch_langs.append(lang)

    # req_skill_hit as a positive signal when a structured required section exists;
    # fall back to skill_score as a proxy for unstructured JDs.
    req_coverage_bonus = req_skill_hit if (has_req_section and req_section) else skill_score

    raw = (
        0.26 * skill_score +          # direct resume-skill match — strongest ATS signal
        0.20 * role_component +       # target-role keyword coverage
        0.16 * semantic_component +   # concept-vector similarity
        0.12 * ontology_component +   # ontology cluster coverage
        0.10 * resume_component +     # technical overlap between resume and JD
        0.10 * req_coverage_bonus +   # structured required-section skill coverage
        0.06 * title_component        # exact role-name in title bonus
    )

    if _NEW_GRAD_RE.search(job_text):
        raw = min(1.0, raw * 1.15)

    if _H1B_POS_RE.search(desc):
        raw = min(1.0, raw * 1.08)

    caps: list[str] = []

    # ── Hard disqualifiers (score forced to near-zero) ────────────────────────
    # PhD required — user explicitly does not want these roles.
    if phd_required and not resume_has_phd:
        raw = min(raw, 0.18)
        caps.append("PhD required — hard reject")

    # ── Strong caps ───────────────────────────────────────────────────────────
    if not ml_intent:
        raw = min(raw * 0.45, 0.28)
        caps.append("no AI/ML/robotics/quant intent")
    elif skill_score < 0.10 and role_component < 0.70:
        raw = min(raw, 0.34)
        caps.append("weak target-role and resume-skill evidence")

    if _NON_TECH_ROLE_RE.search(title_lower):
        raw = min(raw, 0.34)
        caps.append("non-technical AI-adjacent title family")

    # Experience year penalty — new grad applying to senior/staff roles.
    if req_years >= 5:
        raw = min(raw, 0.40)
        caps.append(f"requires {req_years}+ years experience")
    elif req_years >= 3:
        raw = min(raw, 0.58)
        caps.append(f"requires {req_years}+ years experience")

    # Stack mismatch: primary language not on resume.
    # Title-level match = this role IS that language (e.g. "Java Developer") → hard reject.
    _title_lang_mismatch = [
        lang for lang in stack_mismatch_langs
        if re.search(r"\b" + re.escape(lang) + r"\b", title_lower)
    ]
    if _title_lang_mismatch:
        raw = min(raw, 0.28)
        caps.append(f"title-level stack mismatch — {', '.join(_title_lang_mismatch)}")
    elif len(stack_mismatch_langs) >= 2:
        raw = min(raw, 0.38)
        caps.append(f"stack mismatch — required: {', '.join(stack_mismatch_langs[:4])}")
    elif len(stack_mismatch_langs) == 1:
        raw = min(raw, 0.44)
        caps.append(f"stack mismatch — required: {stack_mismatch_langs[0]}")

    # Required-section skill coverage — graded caps, only when an explicit required section exists.
    if has_req_section and req_section and req_max > 8:
        if req_skill_hit < 0.30:
            raw = min(raw, 0.38)
            top_missing = ", ".join(missing_required[:5])
            caps.append(
                f"very low required-skill coverage ({req_skill_hit:.0%}) "
                f"— missing: {top_missing}"
            )
        elif req_skill_hit < 0.50:
            raw = min(raw, 0.55)
            caps.append(f"low required-skill coverage ({req_skill_hit:.0%})")
        elif req_skill_hit < 0.65:
            raw = min(raw, 0.70)
            caps.append(f"moderate required-skill coverage ({req_skill_hit:.0%})")

    # Apply learned boost/penalty patterns (populated by `./autopilot.py learn`).
    # Only active when use_learned_patterns: true in config (behavior section).
    if _USE_LEARNED_PATTERNS:
        for pat in _LEARNED_SCORE_BOOSTS:
            try:
                if re.search(pat, job_text, re.IGNORECASE):
                    raw = min(1.0, raw * 1.12)
                    caps.append(f"learned boost: {pat!r}")
                    break
            except re.error:
                pass
        for pat in _LEARNED_SCORE_PENALTIES:
            try:
                if re.search(pat, job_text, re.IGNORECASE):
                    raw = max(0.0, raw * 0.80)
                    caps.append(f"learned penalty: {pat!r}")
                    break
            except re.error:
                pass

    score = int(round(100.0 * min(1.0, raw)))
    if score >= 75:
        tier = "strong"
    elif score >= 55:
        tier = "review"
    elif score >= 35:
        tier = "weak_review"
    else:
        tier = "reject"

    bd: dict = {
        "score": score,
        "scorecard_points": round(20.0 * role_component, 1),
        "resume_skill_points": round(26.0 * skill_score, 1),
        "semantic_points": round(16.0 * semantic_component, 1),
        "ontology_points": round(12.0 * ontology_component, 1),
        "technical_overlap_points": round(10.0 * resume_component, 1),
        "title_bonus_points": round(6.0 * title_component, 1),
        "required_section_coverage": round(req_skill_hit * 100, 1) if has_req_section else None,
        "missing_required_skills": missing_required[:10] if missing_required else [],
        "required_years": req_years if req_years else None,
        "phd_required": phd_required,
        "stack_mismatch": stack_mismatch_langs if stack_mismatch_langs else [],
        "tier": tier,
        "caps": caps,
        "fit_summary": "",
    }
    bd["fit_summary"] = _build_fallback_fit_summary(bd, job)
    return bd


# Regex for the kinds of numeric "facts" a resume contains. We extract these
# so the LLM can only cite numbers that actually appear in the resume - no
# rounding, no inflating, no fabrication.
_UNIT_WORDS = (
    "requests|users|customers|rows|records|queries|nodes|services|"
    "endpoints|hours|seconds|minutes|days|ms|GB|TB|MB|KB|points|lines|"
    "engineers|teams|repos|commits"
)
_NUMERIC_FACT_RE = re.compile(
    r"""
    (?:
        \$\s?\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?\b                            # $5, $1.2M
      | \b\d+(?:,\d{3})+(?:\s?(?:""" + _UNIT_WORDS + r"""))?              # 1,200 services
      | \b\d+(?:\.\d+)?\s?%                                              # 8%, 40.5%
      | \b\d+(?:\.\d+)?\s?x\b                                            # 2x, 10x
      | \b\d+\s?[-+]\s?(?:\d+\s?)?(?:years?|yrs?)\b                      # 3+ years, 5-7 years
      | \b\d{2,}\s?(?:""" + _UNIT_WORDS + r""")\b                         # 45000 queries
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_resume_numbers(text: str) -> list[str]:
    """Pull every numeric/quantitative token from the resume text.
    Used as the strict allow-list for what the LLM may cite in the email."""
    if not text:
        return []
    found = [m.group(0).strip() for m in _NUMERIC_FACT_RE.finditer(text)]
    # Dedupe preserving order.
    seen, out = set(), []
    for f in found:
        norm = re.sub(r"\s+", "", f.lower())
        if norm in seen:
            continue
        seen.add(norm)
        out.append(f)
    return out


def disallowed_pitch_numbers(pitch: dict, allowed: list[str]) -> list[str]:
    """Return every numeric token in pitch.subject / pitch.body that is NOT
    in the allowed list. Empty result means the LLM stayed honest."""
    text = (pitch.get("subject") or "") + " | " + (pitch.get("body") or "")
    found = [m.group(0).strip() for m in _NUMERIC_FACT_RE.finditer(text)]
    norm_allowed = {re.sub(r"\s+", "", a.lower()) for a in (allowed or [])}
    return [f for f in found if re.sub(r"\s+", "", f.lower()) not in norm_allowed]


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("pypdf required. Run: pip install pypdf") from e
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


# ---------------------------------------------------------------------------
# Resume PDF: extract lines + apply edits (no LLM here)
# ---------------------------------------------------------------------------

def extract_resume_lines(pdf_path: Path) -> tuple[dict, list[dict]]:
    """Return (line_map, payload). line_map keys to PyMuPDF span metadata;
    payload is the JSON-serializable form sent to the LLM."""
    try:
        import fitz  # noqa: F401  - imported here to keep module import lightweight
    except ImportError as e:
        raise RuntimeError("pymupdf required. Run: pip install pymupdf") from e
    import fitz
    doc = fitz.open(str(pdf_path))
    payload: list[dict] = []
    line_map: dict[int, dict] = {}
    lid = 0
    try:
        for page_idx, page in enumerate(doc):
            for block in page.get_text("dict").get("blocks", []):
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    spans = line.get("spans", []) or []
                    if not spans:
                        continue
                    text = "".join(s.get("text", "") for s in spans).strip()
                    if len(text) < 8:
                        continue
                    tmpl = max(spans, key=lambda s: len(s.get("text", "")))
                    cap = max(40, int(len(text) * 1.1))
                    payload.append({"id": lid, "text": text, "max_chars": cap})
                    line_map[lid] = {
                        "page": page_idx,
                        "bbox": tuple(line["bbox"]),
                        "font": tmpl.get("font", "helv"),
                        "size": float(tmpl.get("size", 10.0)),
                        "flags": int(tmpl.get("flags", 0)),
                        "color": int(tmpl.get("color", 0)),
                        "original": text,
                        "cap": cap,
                    }
                    lid += 1
    finally:
        doc.close()
    return line_map, payload


def apply_resume_edits(pdf_path: Path, line_map: dict[int, dict],
                       edits_raw: list[dict], output_pdf: Path) -> Path:
    """Apply the LLM-supplied edits to the PDF, redacting each edited line's
    bbox and re-inserting the replacement at the same coords/style. Caps each
    edit at the original line's max_chars."""
    import fitz
    if not isinstance(edits_raw, list) or not edits_raw:
        raise RuntimeError("no edits to apply")

    edits: dict[int, str] = {}
    for e in edits_raw:
        if not isinstance(e, dict):
            continue
        try:
            eid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        new_text = str(e.get("text", "")).strip()
        if not new_text or eid not in line_map:
            continue
        if new_text == line_map[eid]["original"]:
            continue
        edits[eid] = new_text[: line_map[eid]["cap"]]
    if not edits:
        raise RuntimeError("no usable edits after filtering")

    doc = fitz.open(str(pdf_path))
    try:
        pages_touched: set[int] = set()
        for eid in edits:
            meta = line_map[eid]
            page = doc[meta["page"]]
            page.add_redact_annot(fitz.Rect(*meta["bbox"]), fill=(1, 1, 1))
            pages_touched.add(meta["page"])
        for p in pages_touched:
            doc[p].apply_redactions()

        for eid, new_text in edits.items():
            meta = line_map[eid]
            page = doc[meta["page"]]
            fn = (meta["font"] or "").lower()
            is_bold = bool(meta["flags"] & 16) or "bold" in fn
            is_italic = bool(meta["flags"] & 2) or "italic" in fn or "oblique" in fn
            if "times" in fn or "tiro" in fn or "serif" in fn:
                font_name = "tibo" if is_bold else "tibi" if is_italic else "tiro"
            elif "courier" in fn or "mono" in fn:
                font_name = "cobo" if is_bold else "coit" if is_italic else "cour"
            else:
                font_name = "hebo" if is_bold else "heit" if is_italic else "helv"

            c = meta["color"]
            color = (((c >> 16) & 0xFF) / 255.0,
                     ((c >> 8) & 0xFF) / 255.0,
                     (c & 0xFF) / 255.0)
            x0, y0, x1, y1 = meta["bbox"]
            write_rect = fitz.Rect(x0, y0, x1, y1 + 1.5)
            try:
                overflow = page.insert_textbox(
                    write_rect, new_text,
                    fontname=font_name, fontsize=meta["size"],
                    color=color, align=0,
                )
                if overflow < 0:
                    shorter = new_text[: max(10, len(new_text) - 8)]
                    page.insert_textbox(
                        write_rect, shorter,
                        fontname=font_name, fontsize=meta["size"],
                        color=color, align=0,
                    )
            except Exception as e:
                log.warning("insert_textbox failed (eid=%s): %s", eid, e)
                page.insert_text(
                    (x0, y1 - 1), new_text,
                    fontname="helv", fontsize=meta["size"], color=color,
                )

        while doc.page_count > 1:
            doc.delete_page(doc.page_count - 1)
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_pdf), garbage=4, deflate=True)
    finally:
        doc.close()
    return output_pdf


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT,
    company      TEXT,
    location     TEXT,
    description  TEXT,
    posted_at    TEXT,
    match_score  INTEGER DEFAULT 0,
    skip_reason  TEXT,
    fit_summary  TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new',
    run_tag      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS companies (
    domain       TEXT PRIMARY KEY,
    name         TEXT,
    first_seen   TEXT NOT NULL,
    last_checked TEXT,
    careers_url  TEXT,
    notes        TEXT
);
CREATE TABLE IF NOT EXISTS applications (
    job_id       TEXT PRIMARY KEY REFERENCES jobs(id),
    recruiter    TEXT,
    sent_to      TEXT,
    subject      TEXT,
    sent_at      TEXT NOT NULL,
    pdf_path     TEXT,
    notes        TEXT
);
CREATE TABLE IF NOT EXISTS mail_events (
    message_key   TEXT PRIMARY KEY,
    job_id        TEXT REFERENCES jobs(id),
    received_at   TEXT,
    from_addr     TEXT,
    subject       TEXT,
    classification TEXT,
    confidence    INTEGER DEFAULT 0,
    match_score   INTEGER DEFAULT 0,
    snippet       TEXT,
    processed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_feedback (
    domain        TEXT PRIMARY KEY,
    company_name  TEXT,
    label         TEXT NOT NULL,
    example_desc  TEXT,
    feedback_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_feedback (
    job_id        TEXT PRIMARY KEY REFERENCES jobs(id),
    label         TEXT NOT NULL,
    feedback_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_patterns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type  TEXT NOT NULL,
    pattern       TEXT NOT NULL UNIQUE,
    confidence    REAL DEFAULT 0.8,
    source        TEXT DEFAULT 'llm',
    created_at    TEXT NOT NULL,
    active        INTEGER DEFAULT 1
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()
        self._run_tag: str = ""

    def _ensure_columns(self) -> None:
        jobs_cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "match_score" not in jobs_cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN match_score INTEGER DEFAULT 0")
        if "skip_reason" not in jobs_cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN skip_reason TEXT")
        if "fit_summary" not in jobs_cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN fit_summary TEXT DEFAULT ''")
        if "run_tag" not in jobs_cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN run_tag TEXT DEFAULT ''")
        company_cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "careers_unreachable" not in company_cols:
            self.conn.execute(
                "ALTER TABLE companies ADD COLUMN careers_unreachable INTEGER DEFAULT 0"
            )

    def set_run_tag(self, tag: str) -> None:
        """Set the run identifier for this scraping session. All new/refreshed
        jobs inserted after this call will be tagged so the Excel export can
        highlight them as 'new from latest run'."""
        self._run_tag = tag

    def seen(self, job_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None

    def insert_job(self, job: dict) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (id, source, url, title, company, location, description, posted_at,
                match_score, skip_reason, fit_summary, first_seen, status, run_tag)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], job["source"], job["url"], job.get("title", ""),
             job.get("company", ""), job.get("location", ""),
             (job.get("description") or "")[:8000],
             job.get("posted_at", ""), int(job.get("match_score", 0) or 0),
             job.get("skip_reason", ""),
             job.get("fit_summary", "") or "",
             datetime.now(timezone.utc).isoformat(), "new",
             self._run_tag),
        )
        self.conn.commit()

    def queue_or_refresh_job(self, job: dict) -> None:
        """Insert a reviewable job, or revive an older low-score skipped row.

        We intentionally do not revive jobs skipped by title, visa, repost, or
        experience filters. This only rescues rows whose old skip reason was a
        low/unknown ATS score before richer detail text was available.
        Also refreshes 'new' rows that still have match_score=0 (inserted before
        scoring ran, e.g. from an early seed or partial pipeline run).
        """
        if not self.seen(job["id"]):
            self.insert_job(job)
            return
        self.conn.execute(
            """UPDATE jobs
               SET match_score=?, description=?, fit_summary=?, status='new', skip_reason='',
                   first_seen=?, run_tag=?
               WHERE id=?
                 AND (
                     (status='skipped' AND (
                         skip_reason IS NULL
                         OR trim(skip_reason)=''
                         OR skip_reason LIKE 'ATS score %'
                         OR skip_reason='historical skipped before reason tracking'
                     ))
                     OR (status='new' AND match_score=0)
                 )""",
            (
                int(job.get("match_score", 0) or 0),
                (job.get("description") or "")[:8000],
                job.get("fit_summary", "") or "",
                datetime.now(timezone.utc).isoformat(),
                self._run_tag,
                job["id"],
            ),
        )
        self.conn.commit()

    def mark(self, job_id: str, status: str, reason: str = "") -> None:
        if reason:
            self.conn.execute(
                "UPDATE jobs SET status=?, skip_reason=? WHERE id=?",
                (status, reason, job_id),
            )
        elif status != "skipped":
            self.conn.execute(
                "UPDATE jobs SET status=?, skip_reason='' WHERE id=?",
                (status, job_id),
            )
        else:
            self.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        self.conn.commit()

    def log_mail_event(self, event: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO mail_events
               (message_key, job_id, received_at, from_addr, subject,
                classification, confidence, match_score, snippet, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["message_key"],
                event.get("job_id"),
                event.get("received_at", ""),
                event.get("from_addr", ""),
                event.get("subject", ""),
                event.get("classification", ""),
                int(event.get("confidence", 0) or 0),
                int(event.get("match_score", 0) or 0),
                (event.get("snippet") or "")[:1000],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def mail_event_seen(self, message_key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM mail_events WHERE message_key=?", (message_key,)
        ).fetchone() is not None

    def new_jobs(self, limit: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM jobs WHERE status='new' ORDER BY match_score DESC, first_seen DESC LIMIT ?",
            (limit,)
        ).fetchall())

    def watch(self, domain: str, name: str, careers_url: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO companies (domain, name, first_seen, careers_url)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                   name=excluded.name,
                   careers_url=CASE
                       WHEN excluded.careers_url IS NOT NULL AND excluded.careers_url != ''
                       THEN excluded.careers_url
                       ELSE companies.careers_url
                   END""",
            (domain.lower(), name, now, careers_url),
        )
        self.conn.commit()

    def list_watched(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM companies ORDER BY first_seen DESC").fetchall())

    def touch_company(self, domain: str) -> None:
        self.conn.execute(
            "UPDATE companies SET last_checked=? WHERE domain=?",
            (datetime.now(timezone.utc).isoformat(), domain.lower()),
        )
        self.conn.commit()

    def set_careers_url(self, domain: str, careers_url: str) -> None:
        self.conn.execute(
            "UPDATE companies SET careers_url=? WHERE domain=?",
            (careers_url, domain.lower()),
        )
        self.conn.commit()

    def mark_careers_unreachable(self, domain: str) -> None:
        self.conn.execute(
            "UPDATE companies SET careers_unreachable=1 WHERE domain=?", (domain.lower(),)
        )
        self.conn.commit()

    def clear_careers_unreachable(self, domain: str) -> None:
        self.conn.execute(
            "UPDATE companies SET careers_unreachable=0 WHERE domain=?", (domain.lower(),)
        )
        self.conn.commit()

    def unwatch(self, domain: str) -> None:
        self.conn.execute("DELETE FROM companies WHERE domain=?", (domain.lower(),))
        self.conn.commit()

    def log_application(self, job_id: str, recruiter: str, sent_to: str,
                        subject: str, pdf_path: str, notes: str = "") -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO applications
               (job_id, recruiter, sent_to, subject, sent_at, pdf_path, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, recruiter, sent_to, subject,
             datetime.now(timezone.utc).isoformat(), pdf_path, notes),
        )
        self.conn.commit()

    def stats(self) -> dict:
        c = self.conn.execute
        return {
            "total_jobs": c("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "applied":    c("SELECT COUNT(*) FROM jobs WHERE status='applied'").fetchone()[0],
            "rejected":   c("SELECT COUNT(*) FROM jobs WHERE status='rejected'").fetchone()[0],
            "interview":  c("SELECT COUNT(*) FROM jobs WHERE status='interview'").fetchone()[0],
            "assessment": c("SELECT COUNT(*) FROM jobs WHERE status='assessment'").fetchone()[0],
            "offer":      c("SELECT COUNT(*) FROM jobs WHERE status='offer'").fetchone()[0],
            "withdrawn":  c("SELECT COUNT(*) FROM jobs WHERE status='withdrawn'").fetchone()[0],
            "skipped":    c("SELECT COUNT(*) FROM jobs WHERE status='skipped'").fetchone()[0],
            "ready_for_review": c("SELECT COUNT(*) FROM jobs WHERE status='ready_for_review'").fetchone()[0],
            "error":      c("SELECT COUNT(*) FROM jobs WHERE status='error'").fetchone()[0],
            "new":        c("SELECT COUNT(*) FROM jobs WHERE status='new'").fetchone()[0],
            "watched_companies": c("SELECT COUNT(*) FROM companies").fetchone()[0],
        }

    # ── Feedback / RLHF ──────────────────────────────────────────────────────

    def record_company_feedback(
        self, domain: str, company: str, label: str, example_desc: str = ""
    ) -> None:
        self.conn.execute(
            """INSERT INTO company_feedback (domain, company_name, label, example_desc, feedback_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                   label=excluded.label,
                   example_desc=COALESCE(NULLIF(excluded.example_desc,''), company_feedback.example_desc),
                   feedback_at=excluded.feedback_at""",
            (domain.lower(), company, label, (example_desc or "")[:1000],
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_company_label(self, domain: str) -> str | None:
        row = self.conn.execute(
            "SELECT label FROM company_feedback WHERE domain=?", (domain.lower(),)
        ).fetchone()
        return row["label"] if row else None

    def record_job_feedback(self, job_id: str, label: str) -> None:
        self.conn.execute(
            """INSERT INTO job_feedback (job_id, label, feedback_at)
               VALUES (?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET label=excluded.label, feedback_at=excluded.feedback_at""",
            (job_id, label, datetime.now(timezone.utc).isoformat()),
        )
        if label == "not_relevant":
            self.conn.execute(
                """UPDATE jobs SET status='skipped', skip_reason='user feedback: not relevant'
                   WHERE id=? AND status NOT IN ('applied','offer','interview','assessment')""",
                (job_id,),
            )
        self.conn.commit()

    def get_staffing_examples(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            """SELECT domain, company_name, example_desc FROM company_feedback
               WHERE label='staffing' AND example_desc IS NOT NULL AND example_desc != ''"""
        ).fetchall()]

    def add_learned_pattern(
        self, pattern_type: str, pattern: str,
        confidence: float = 0.8, source: str = "llm",
    ) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO learned_patterns (pattern_type, pattern, confidence, source, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (pattern_type, pattern, confidence, source,
                 datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_learned_patterns(self, pattern_type: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT pattern FROM learned_patterns WHERE pattern_type=? AND active=1",
            (pattern_type,),
        ).fetchall()]

    def feedback_summary(self) -> dict:
        c = self.conn.execute
        return {
            "staffing": c("SELECT COUNT(*) FROM company_feedback WHERE label='staffing'").fetchone()[0],
            "not_staffing": c("SELECT COUNT(*) FROM company_feedback WHERE label='not_staffing'").fetchone()[0],
            "not_relevant": c("SELECT COUNT(*) FROM job_feedback WHERE label='not_relevant'").fetchone()[0],
            "relevant": c("SELECT COUNT(*) FROM job_feedback WHERE label='relevant'").fetchone()[0],
            "learned_patterns": c("SELECT COUNT(*) FROM learned_patterns WHERE active=1").fetchone()[0],
        }

    def import_excel_feedback(self, path: "Path") -> int:
        """Read the Feedback column from jobs.xlsx and persist new labels to DB.
        Returns count of new/updated feedback items."""
        try:
            import openpyxl
        except ImportError:
            return 0
        if not path.exists():
            return 0
        try:
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        except Exception as e:
            log.warning("import_excel_feedback: could not open %s: %s", path, e)
            return 0

        _STAFFING_LABELS = {"staffing", "agency", "third party", "third-party", "recruiter"}
        _OK_LABELS       = {"not staffing", "not_staffing", "ok", "direct", "ok company"}
        _IRRELEVANT      = {"irrelevant", "not relevant", "not_relevant", "skip", "bad", "no"}
        _RELEVANT        = {"relevant", "good", "keep", "yes"}

        count = 0
        try:
            for sheet_name in wb.sheetnames:
                if sheet_name == "Summary":
                    continue
                ws = wb[sheet_name]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if not row or len(row) < 17:
                        continue
                    feedback = str(row[16] or "").strip().lower()
                    if not feedback:
                        continue
                    url     = str(row[9] or "").strip()
                    company = str(row[4] or "").strip()
                    job_row = (
                        self.conn.execute("SELECT id, description FROM jobs WHERE url=?", (url,)).fetchone()
                        if url else None
                    )
                    if feedback in _STAFFING_LABELS:
                        domain = slug_domain(company)
                        if domain:
                            desc = (dict(job_row)["description"] or "") if job_row else ""
                            self.record_company_feedback(domain, company, "staffing", desc)
                            count += 1
                    elif feedback in _OK_LABELS:
                        domain = slug_domain(company)
                        if domain:
                            self.record_company_feedback(domain, company, "not_staffing")
                            count += 1
                    elif feedback in _IRRELEVANT:
                        if job_row:
                            self.record_job_feedback(dict(job_row)["id"], "not_relevant")
                            count += 1
                    elif feedback in _RELEVANT:
                        if job_row:
                            self.record_job_feedback(dict(job_row)["id"], "relevant")
                            count += 1
        finally:
            wb.close()
        if count:
            log.info("import_excel_feedback: %d new feedback items from %s", count, path)
        return count

    def export_excel(self, path: Path, latest_run_only: bool = False) -> None:
        """Write (or overwrite) an Excel workbook at *path*.

        Sheet layout (default)
        ──────────────────────
        • Sheet 1  "Summary"      — overall totals + one row per day
        • Sheet 2+ "YYYY-MM-DD"   — every job found on that date (newest date first)

        When latest_run_only=True a single "This Run" sheet is written with only
        the jobs discovered in the most recent run_tag, sorted by score.
        """
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            log.warning("openpyxl not installed — skipping Excel export. "
                        "Run: pip install openpyxl")
            return

        from collections import defaultdict

        STATUS_COLORS = {
            "offer":            "FFB6D7A8",
            "interview":        "FFB4C7E7",
            "assessment":       "FFD9EAD3",
            "applied":          "FF90EE90",
            "ready_for_review": "FFFFFF99",
            "new":              "FFCCE5FF",
            "rejected":         "FFF4CCCC",
            "withdrawn":        "FFEADCF8",
            "skipped":          "FFD3D3D3",
            "error":            "FFFFCCCC",
        }
        STATUSES = [
            "offer", "interview", "assessment", "applied",
            "ready_for_review", "new", "rejected", "withdrawn", "skipped", "error",
        ]

        # ── shared style helpers ──────────────────────────────────────────────
        H_FONT  = Font(bold=True, color="FFFFFFFF")
        H_FILL  = PatternFill("solid", fgColor="FF2F5496")
        CENTER  = Alignment(horizontal="center", vertical="center")
        TOP     = Alignment(vertical="top", wrap_text=False)

        def style_header_row(ws, headers, col_widths):
            for c, (hdr, w) in enumerate(zip(headers, col_widths), 1):
                cell = ws.cell(row=1, column=c, value=hdr)
                cell.font = H_FONT
                cell.fill = H_FILL
                cell.alignment = CENTER
                ws.column_dimensions[get_column_letter(c)].width = w
            ws.row_dimensions[1].height = 18
            ws.freeze_panes = "A2"

        _FB_FILL      = PatternFill("solid", fgColor="FFFFF2CC")  # light yellow = editable
        _NEW_RUN_FILL = PatternFill("solid", fgColor="FFFFD966")  # gold = new from latest run

        def write_job_row(ws, row_num, r, is_latest_run: bool = False):
            status = r.get("status", "new") or "new"
            # Latest-run new/unreviewed jobs get a gold highlight so they stand out.
            if is_latest_run and status in ("new", "ready_for_review", "skipped"):
                fill = _NEW_RUN_FILL
            else:
                fill = PatternFill("solid", fgColor=STATUS_COLORS.get(status, "FFFFFFFF"))
            score  = r.get("match_score") or 0
            fb     = _feedback_for(r.get("id", ""), r.get("company", ""))
            status_display = f"[NEW RUN] {status}" if is_latest_run else status
            values = [
                status_display,
                f"{score}/100",
                r.get("fit_summary", "") or "",
                r.get("title", ""),
                r.get("company", ""),
                r.get("location", ""),
                r.get("source", ""),
                r.get("posted_at", ""),
                r.get("first_seen", ""),
                r.get("url", ""),
                r.get("skip_reason", ""),
                r.get("sent_to", ""),
                r.get("sent_at", ""),
                r.get("method", ""),
                r.get("recruiter", ""),
                r.get("notes", ""),
                fb,
            ]
            for c, val in enumerate(values, 1):
                cell = ws.cell(row=row_num, column=c, value=val or "")
                if c == 17:
                    cell.fill      = _FB_FILL
                    cell.alignment = CENTER
                else:
                    cell.fill      = fill
                    cell.alignment = TOP
                if c == 10 and val:
                    cell.hyperlink = val
                    cell.font = Font(color="FF0563C1", underline="single")

        # Pre-load feedback from DB for the Feedback column
        _company_labels = {
            r["domain"]: r["label"]
            for r in self.conn.execute("SELECT domain, label FROM company_feedback").fetchall()
        }
        _job_labels = {
            r["job_id"]: r["label"]
            for r in self.conn.execute("SELECT job_id, label FROM job_feedback").fetchall()
        }
        _LABEL_DISPLAY = {
            "staffing": "staffing", "not_staffing": "ok",
            "not_relevant": "irrelevant", "relevant": "relevant",
        }

        def _feedback_for(job_id: str, company: str) -> str:
            jl = _job_labels.get(job_id or "")
            if jl:
                return _LABEL_DISPLAY.get(jl, jl)
            cl = _company_labels.get(slug_domain(company or ""))
            if cl:
                return _LABEL_DISPLAY.get(cl, cl)
            return ""

        JOB_HEADERS = [
            "Status", "Score /100", "Fit Summary (AI)", "Title", "Company", "Location",
            "Source", "Posted At", "Found At", "Apply URL",
            "Reason / Mail Note", "Sent To", "Applied At", "Method", "Recruiter", "Notes",
            "Feedback ▸ staffing | ok | irrelevant | relevant",
        ]
        JOB_WIDTHS = [16, 10, 60, 36, 28, 22, 10, 20, 20, 50, 42, 36, 20, 22, 22, 40, 28]

        # ── latest-run-only mode: single sheet, fresh every run ───────────────
        if latest_run_only:
            _lt_row = self.conn.execute(
                "SELECT MAX(run_tag) AS t FROM jobs WHERE run_tag != ''"
            ).fetchone()
            latest_tag = (_lt_row["t"] if _lt_row and _lt_row["t"] else "") or ""
            if latest_tag:
                run_rows = self.conn.execute(
                    """
                    SELECT
                        j.id, j.status, j.match_score, j.fit_summary, j.title, j.company,
                        j.location, j.source, j.posted_at, j.first_seen, j.url, j.skip_reason,
                        a.sent_to, a.sent_at, a.subject AS method, a.recruiter, a.notes,
                        j.run_tag
                    FROM jobs j
                    LEFT JOIN applications a ON a.job_id = j.id
                    WHERE j.run_tag = ?
                      AND j.status NOT IN ('skipped')
                    ORDER BY j.match_score DESC, j.first_seen DESC
                    """,
                    (latest_tag,),
                ).fetchall()
            else:
                run_rows = []
            wb = openpyxl.Workbook()
            ws = wb.active
            run_date = (latest_tag or "")[:10]
            ws.title = f"Run {run_date}" if run_date else "This Run"
            style_header_row(ws, JOB_HEADERS, JOB_WIDTHS)
            ws.cell(row=1, column=17).fill = PatternFill("solid", fgColor="FF548235")
            for row_num, r in enumerate(run_rows, 2):
                write_job_row(ws, row_num, dict(r), is_latest_run=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            wb.save(str(path))
            log.info("Excel (latest-run) export: %s  (%d jobs from run %s)",
                     path, len(run_rows), run_date)
            return

        # ── fetch all rows with date tag ──────────────────────────────────────
        all_rows = self.conn.execute(
            """
            SELECT
                j.id, j.status, j.match_score, j.fit_summary, j.title, j.company, j.location,
                j.source, j.posted_at, j.first_seen, j.url, j.skip_reason,
                a.sent_to, a.sent_at, a.subject AS method, a.recruiter, a.notes,
                substr(j.first_seen, 1, 10) AS found_date,
                j.run_tag
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            ORDER BY j.first_seen DESC, j.match_score DESC
            """
        ).fetchall()

        # Group by date and identify the latest run tag; deduplicate by URL.
        by_date: dict[str, list[dict]] = defaultdict(list)
        latest_run_tag = ""
        seen_urls: set[str] = set()
        for row in all_rows:
            r = dict(row)
            url = (r.get("url") or "").strip()
            if url:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
            by_date[r.get("found_date", "unknown")].append(r)
            tag = r.get("run_tag") or ""
            if tag and tag > latest_run_tag:
                latest_run_tag = tag

        # ── workbook ─────────────────────────────────────────────────────────
        wb = openpyxl.Workbook()

        # ════════════════════════════════════════════════════════════════════
        # Sheet 1 — Summary
        # ════════════════════════════════════════════════════════════════════
        ws_sum = wb.active
        ws_sum.title = "Summary"

        # Overall totals block (rows 1-3)
        total_stats = self.stats()
        TILE_LABELS = [
            "Total Found", "Offer", "Interview", "Assessment", "Applied",
            "Ready for Review", "New", "Rejected", "Skipped", "Error",
        ]
        TILE_KEYS = [
            "total_jobs", "offer", "interview", "assessment", "applied",
            "ready_for_review", "new", "rejected", "skipped", "error",
        ]
        TILE_COLORS = [
            "FF2F5496", "FFB6D7A8", "FFB4C7E7", "FFD9EAD3", "FF90EE90",
            "FFFFFF99", "FFCCE5FF", "FFF4CCCC", "FFD3D3D3", "FFFFCCCC",
        ]
        TILE_FONT_C = ["FFFFFFFF"] + ["FF000000"] * (len(TILE_LABELS) - 1)

        for c, (label, key, bg, fc) in enumerate(
                zip(TILE_LABELS, TILE_KEYS, TILE_COLORS, TILE_FONT_C), 1):
            lc = ws_sum.cell(row=1, column=c, value=label)
            lc.font      = Font(bold=True, size=9, color=fc)
            lc.fill      = PatternFill("solid", fgColor=bg)
            lc.alignment = CENTER

            vc = ws_sum.cell(row=2, column=c, value=total_stats.get(key, 0))
            vc.font      = Font(bold=True, size=16, color=fc)
            vc.fill      = PatternFill("solid", fgColor=bg)
            vc.alignment = CENTER
            ws_sum.column_dimensions[get_column_letter(c)].width = 18
        ws_sum.row_dimensions[1].height = 14
        ws_sum.row_dimensions[2].height = 28

        # Spacer row 3
        ws_sum.row_dimensions[3].height = 8

        # Daily breakdown table starting at row 4
        DAY_HEADERS = [
            "Date", "Total Found", "Offer", "Interview", "Assessment", "Applied",
            "Ready for Review", "New", "Rejected", "Skipped", "Error",
        ]
        DAY_WIDTHS = [14, 13, 8, 10, 12, 10, 18, 8, 10, 10, 8]
        for c, (hdr, w) in enumerate(zip(DAY_HEADERS, DAY_WIDTHS), 1):
            cell = ws_sum.cell(row=4, column=c, value=hdr)
            cell.font      = H_FONT
            cell.fill      = H_FILL
            cell.alignment = CENTER
            ws_sum.column_dimensions[get_column_letter(c)].width = w
        ws_sum.row_dimensions[4].height = 16

        for r_num, date in enumerate(sorted(by_date.keys(), reverse=True), 5):
            jobs      = by_date[date]
            counts    = {s: sum(1 for j in jobs if j["status"] == s) for s in STATUSES}
            row_vals = [
                date, len(jobs), counts["offer"], counts["interview"],
                counts["assessment"], counts["applied"], counts["ready_for_review"],
                counts["new"], counts["rejected"], counts["skipped"], counts["error"],
            ]
            for c, val in enumerate(row_vals, 1):
                cell = ws_sum.cell(row=r_num, column=c, value=val)
                cell.alignment = CENTER
                if c == 1:
                    cell.font = Font(bold=True)

        ws_sum.freeze_panes = "A5"

        # ════════════════════════════════════════════════════════════════════
        # One sheet per day
        # ════════════════════════════════════════════════════════════════════
        for date in sorted(by_date.keys(), reverse=True):
            ws = wb.create_sheet(title=date)
            style_header_row(ws, JOB_HEADERS, JOB_WIDTHS)
            # Color the Feedback header distinctively (green)
            ws.cell(row=1, column=17).fill = PatternFill("solid", fgColor="FF548235")
            # Mark header for latest-run indicator
            if latest_run_tag:
                ws.cell(row=1, column=1).value = "Status  (gold = new this run)"

            def _sort_key(j):
                is_new_run = bool(latest_run_tag and (j.get("run_tag") or "") == latest_run_tag)
                raw_status = (j.get("status") or "new")
                # Strip the "[NEW RUN] " prefix if present for status index lookup
                status = raw_status.replace("[NEW RUN] ", "") if raw_status.startswith("[NEW RUN]") else raw_status
                status_idx = STATUSES.index(status) if status in STATUSES else 99
                return (
                    0 if is_new_run else 1,   # latest-run jobs float to top
                    status_idx,
                    -(j.get("match_score") or 0),
                )

            jobs = sorted(by_date[date], key=_sort_key)
            for row_num, r in enumerate(jobs, 2):
                is_new_run = bool(latest_run_tag and (r.get("run_tag") or "") == latest_run_tag)
                write_job_row(ws, row_num, r, is_latest_run=is_new_run)

        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(path))
        log.info("Excel export updated: %s  (%d rows, %d daily sheets)",
                 path, len(all_rows), len(by_date))


# ---------------------------------------------------------------------------
# Feedback / RLHF synthesis
# ---------------------------------------------------------------------------

def synthesize_staffing_patterns(grok: "Grok", db: "DB") -> int:
    """Ask the LLM to extract new filter patterns from accumulated staffing examples.
    Returns number of new patterns added to the DB."""
    examples = db.get_staffing_examples()
    if len(examples) < 3:
        log.debug("synthesize_staffing_patterns: only %d examples, need 3+", len(examples))
        return 0

    examples_payload = [
        {
            "company": e["company_name"],
            "description_snippet": (e["example_desc"] or "")[:400],
        }
        for e in examples[:15]
    ]
    system = (
        "You are a regex pattern engineer. Your task is to improve a job-board filter "
        "that blocks staffing agencies and third-party recruiters posting on behalf of "
        "clients. Patterns are applied with Python re.search(pattern, description, "
        "re.IGNORECASE). Each pattern MUST match text that real direct employers (Anthropic, "
        "Google, Stripe, Figma, Databricks, Waymo, etc.) would NEVER write — only recruiting "
        "firms, staffing agencies, or contractors would use it. "
        "FORBIDDEN patterns: single common words (client, clients, contract, solutions, services, "
        "build, design, deploy, manage, deliver, project, system, experience, seeking, looking). "
        "FORBIDDEN: action verbs, skill requirements, work-type labels (remote/hybrid/onsite). "
        "REQUIRED: patterns must be multi-word phrases unique to middleman/agency language such as "
        "'on behalf of our client', 'our client is looking', 'placed at client site', 'corp to corp', "
        "'W2 or C2C', 'we are a staffing firm', 'candidates will be placed', etc."
    )
    user = (
        "The following companies were confirmed by the user to be staffing agencies or "
        "third-party contractors (not direct employers):\n\n"
        f"{json.dumps(examples_payload, indent=2)}\n\n"
        "Analyze ONLY phrases that are exclusive to middleman/agency language — multi-word "
        "phrases a direct employer like Google or Amazon would never write. "
        "Do NOT generate patterns for: common verbs (build/deploy/manage), job requirements "
        "(experience in X), work location (remote/hybrid), or single nouns (client/solution). "
        "Generate 2-4 tight, specific Python regex patterns. "
        'Return ONLY valid JSON: {"patterns": ["pattern1", "pattern2", ...]}'
    )
    try:
        result = grok.chat_json(system, user)
        patterns = result.get("patterns") or []
        added = 0
        for pat in patterns:
            if not isinstance(pat, str) or not pat.strip():
                continue
            try:
                re.compile(pat)  # validate before storing
                if db.add_learned_pattern("staffing_desc", pat.strip(), confidence=0.85):
                    added += 1
                    log.info("synthesize_staffing_patterns: new pattern added: %r", pat.strip())
            except re.error as e:
                log.warning("synthesize_staffing_patterns: bad pattern %r — %s", pat, e)
        return added
    except Exception as e:
        log.warning("synthesize_staffing_patterns failed: %s", e)
        return 0


def synthesize_score_adjustments(grok: "Grok", db: "DB") -> tuple[int, int]:
    """Learn score boost/penalty patterns from user's relevant/irrelevant job feedback.

    Returns (n_boosts_added, n_penalties_added).
    Needs ≥2 examples in each direction before attempting synthesis."""
    relevant_rows = db.conn.execute(
        """SELECT j.title, j.company, j.description, j.match_score
           FROM job_feedback f JOIN jobs j ON j.id = f.job_id
           WHERE f.label = 'relevant'
           ORDER BY j.match_score ASC LIMIT 12"""
    ).fetchall()
    irrelevant_rows = db.conn.execute(
        """SELECT j.title, j.company, j.description, j.match_score
           FROM job_feedback f JOIN jobs j ON j.id = f.job_id
           WHERE f.label = 'not_relevant'
           ORDER BY j.match_score DESC LIMIT 12"""
    ).fetchall()

    if len(relevant_rows) < 2 and len(irrelevant_rows) < 2:
        log.debug("synthesize_score_adjustments: too few feedback examples")
        return 0, 0

    def _row_payload(rows) -> list[dict]:
        return [
            {
                "title": r["title"],
                "company": r["company"],
                "score": r["match_score"],
                "snippet": (r["description"] or "")[:300],
            }
            for r in rows
        ]

    system = (
        "You are calibrating a resume-job match scorer for a new grad ML/data-science "
        "candidate. You'll see jobs the user marked WANTED (but the scorer undervalued) "
        "and jobs marked UNWANTED (but the scorer overvalued). Extract regex patterns "
        "that reliably predict each direction. Patterns are run with Python re.search() "
        "against the combined job title + description (case-insensitive). "
        "Keep patterns specific — avoid patterns that match both good and bad jobs."
    )
    user_parts = []
    if relevant_rows:
        user_parts.append(
            "WANTED jobs (user liked, but scorer gave low score):\n"
            + json.dumps(_row_payload(relevant_rows), indent=2)
        )
    if irrelevant_rows:
        user_parts.append(
            "UNWANTED jobs (user disliked, but scorer gave high score):\n"
            + json.dumps(_row_payload(irrelevant_rows), indent=2)
        )
    user_parts.append(
        'Return ONLY valid JSON with two arrays: '
        '{"boost_patterns": ["pattern1", ...], "penalty_patterns": ["pattern1", ...]}'
    )
    user = "\n\n".join(user_parts)

    try:
        result = grok.chat_json(system, user)
        boosts    = result.get("boost_patterns") or []
        penalties = result.get("penalty_patterns") or []
        n_boost = n_penalty = 0
        for pat in boosts:
            if not isinstance(pat, str) or not pat.strip():
                continue
            try:
                re.compile(pat)
                if db.add_learned_pattern("score_boost", pat.strip(), confidence=0.75):
                    n_boost += 1
                    log.info("score_boost pattern added: %r", pat.strip())
            except re.error as e:
                log.warning("bad boost pattern %r — %s", pat, e)
        for pat in penalties:
            if not isinstance(pat, str) or not pat.strip():
                continue
            try:
                re.compile(pat)
                if db.add_learned_pattern("score_penalty", pat.strip(), confidence=0.75):
                    n_penalty += 1
                    log.info("score_penalty pattern added: %r", pat.strip())
            except re.error as e:
                log.warning("bad penalty pattern %r — %s", pat, e)
        load_learned_score_patterns(db)  # flush module cache
        return n_boost, n_penalty
    except Exception as e:
        log.warning("synthesize_score_adjustments failed: %s", e)
        return 0, 0


# ---------------------------------------------------------------------------
# Grok client
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from any LLM response."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def llm_title_match(grok, resume: str, roles: list[dict], job: dict) -> bool:
    """Binary LLM gate for LinkedIn — checks title alone, or title + qualifications
    when a JD is already available (e.g. after enrichment or from RSS snippet).
    Fails open (True) on any error so good jobs are never accidentally dropped.
    """
    title   = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    role_kws = "; ".join(r.get("keywords", "") for r in (roles or [])[:6])
    desc = (job.get("description") or "").strip()

    # Extract required / preferred qualifications from JD when available
    quals_text = ""
    if desc:
        m = re.search(
            r"(?:required|minimum|preferred|desired|basic)\s+qualifications?\s*[:\-]?\s*(.*?)(?:\n\s*\n|$)",
            desc, re.I | re.DOTALL,
        )
        quals_text = m.group(1).strip() if m else desc[:900]

    # Pull candidate skills from the resume, falling back to the whole resume.
    # A missed regex used to send the gate no candidate context at all, which
    # made it judge on title alone.
    resume_skills = ""
    m = re.search(
        r"(?:skills?|technologies?|tools?|expertise)\s*[:\-]?\s*(.*?)(?:\n\s*\n|$)",
        resume, re.I | re.DOTALL,
    )
    if m:
        resume_skills = m.group(1)[:2000].strip()
    if len(resume_skills) < 80:
        resume_skills = (resume or "")[:4000].strip()

    system = (
        "You are a job relevance pre-filter. "
        'Reply ONLY with JSON {"relevant": true} or {"relevant": false}. No other text.'
    )
    parts = [
        f"Seeking: {role_kws}",
        f"Job: {title} at {company}",
    ]
    if quals_text:
        parts.append(f"Qualifications/Requirements:\n{quals_text}")
    if resume_skills:
        parts.append(f"Candidate skills:\n{resume_skills}")
    parts.append("Is this job potentially a match for the candidate?")
    user = "\n\n".join(parts)

    try:
        result = grok.chat_json(system, user, max_tokens=20, timeout=15)
        return bool(result.get("relevant", True))
    except Exception:
        return True  # fail open


class Grok:
    """OpenAI-compatible chat client. Works with xAI, OpenRouter, Mistral, DeepSeek, etc."""

    def __init__(self, api_key: str, model: str = "grok-4",
                 base_url: str = "https://api.x.ai/v1",
                 extra_headers: dict | None = None,
                 live_search: bool = False,
                 name: str = ""):
        self.api_key = api_key
        self.model = model
        bu = (base_url or "https://api.x.ai/v1").rstrip("/")
        if not bu.endswith("/v1") and "/v1" not in bu:
            bu = bu + "/v1"
        self.url = bu + "/chat/completions"
        self.extra_headers = extra_headers or {}
        self.live_search = live_search
        self.name = name or "Grok"

    def chat(self, system: str, user: str, json_mode: bool = False,
             timeout: int = 120, max_tokens: int = 300,
             search: bool | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        # gpt-oss models emit reasoning tokens that count against the output
        # budget. Keep reasoning minimal and guarantee headroom for the answer,
        # otherwise small-budget callers (the title gate asks for 20 tokens)
        # get an empty content field and fall through to the next provider.
        if "gpt-oss" in self.model:
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max(max_tokens, 512)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        for attempt in range(3):
            r = requests.post(self.url, headers=headers, json=body, timeout=timeout)
            if r.status_code == 429:
                # Raise immediately so MultiLLMClient can fall back to the next provider.
                # Do not sleep here — let the caller decide whether to retry or switch.
                raise RuntimeError(f"LLM rate-limited (429): {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"LLM error {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"].strip()
        raise RuntimeError("LLM request failed after 3 attempts")

    def chat_json(self, system: str, user: str, **kw) -> dict:
        return _parse_json_response(self.chat(system, user, json_mode=True, **kw))


class GeminiClient:
    """Google Gemini REST client (free tier: gemini-2.0-flash — 15 RPM, 1M tokens/day)."""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash", name: str = ""):
        self.api_key = api_key
        self.model = model
        self.url = f"{self.BASE}/{model}:generateContent"
        self.name = name or "Gemini"

    def chat(self, system: str, user: str, max_tokens: int = 300,
             timeout: int = 60, **_kw) -> str:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max_tokens,
            },
        }
        for attempt in range(3):
            r = requests.post(
                self.url, params={"key": self.api_key},
                json=body, timeout=timeout,
            )
            if r.status_code == 429:
                raise RuntimeError(f"Gemini rate-limited (429): {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        raise RuntimeError("Gemini request failed after 3 attempts")

    def chat_json(self, system: str, user: str, **kw) -> dict:
        return _parse_json_response(self.chat(system, user, **kw))


class MultiLLMClient:
    """Tries LLM providers in order; falls back to the next on any error.

    All providers must expose chat() and chat_json() with the same signature
    as Grok. The first provider that succeeds wins; if all fail, raises RuntimeError.
    Rate-limited providers are cooled down for RATE_LIMIT_COOLDOWN seconds so
    they are not retried on every subsequent call within the same run.
    """

    RATE_LIMIT_COOLDOWN = 120  # seconds to skip a provider after a 429

    def __init__(self, providers: list):
        if not providers:
            raise ValueError("MultiLLMClient needs at least one provider")
        self.providers = providers
        # maps provider index -> monotonic time after which it's eligible again
        self._backoff_until: dict[int, float] = {}

    def _active_providers(self):
        now = time.monotonic()
        return [(i, p) for i, p in enumerate(self.providers)
                if now >= self._backoff_until.get(i, 0)]

    def _record_failure(self, i: int, e: Exception):
        name = getattr(self.providers[i], "name", type(self.providers[i]).__name__)
        is_rate_limit = "429" in str(e)
        if is_rate_limit:
            self._backoff_until[i] = time.monotonic() + self.RATE_LIMIT_COOLDOWN
            log.warning("LLM provider %s rate-limited — cooling down for %ds", name, self.RATE_LIMIT_COOLDOWN)
        return is_rate_limit

    def _call(self, method: str, system: str, user: str, **kw):
        last_exc: Exception = RuntimeError("no providers")
        candidates = self._active_providers()
        if not candidates:
            # All providers are cooled down — raise immediately so callers fall back to semantic scoring.
            soonest = min(self._backoff_until[i] for i in self._backoff_until) - time.monotonic()
            log.warning("All LLM providers rate-limited (soonest ready in %.0fs) — falling back to semantic", soonest)
            raise RuntimeError(f"All LLM providers rate-limited; soonest ready in {soonest:.0f}s")
        for i, p in candidates:
            try:
                return getattr(p, method)(system, user, **kw)
            except Exception as e:
                self._record_failure(i, e)
                remaining = len(candidates) - candidates.index((i, p)) - 1
                log.warning("LLM provider %s failed (%s)%s",
                            getattr(p, "name", type(p).__name__), e,
                            f" — trying next ({remaining} left)" if remaining else " — no more providers")
                last_exc = e
        raise RuntimeError(f"All {len(self.providers)} LLM providers failed. Last: {last_exc}")

    def chat(self, system: str, user: str, **kw) -> str:
        return self._call("chat", system, user, **kw)

    def chat_json(self, system: str, user: str, **kw) -> dict:
        return self._call("chat_json", system, user, **kw)


# ---------------------------------------------------------------------------
# Recruiter / email discovery
# ---------------------------------------------------------------------------

GENERIC_INBOXES = ["careers", "jobs", "hiring", "talent", "recruiting", "people"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def slug_domain(name_or_url: str) -> str:
    s = (name_or_url or "").lower()
    s = re.sub(r"^https?://", "", s).split("/")[0].replace("www.", "")
    if "." in s:
        return s
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s + ".com" if s else ""


def candidate_emails(recruiter_name: str, domain: str, careers_email: str = "") -> list[str]:
    out: list[str] = []
    if careers_email and EMAIL_RE.match(careers_email):
        out.append(careers_email.lower())
    name = re.sub(r"[^a-z\s\-]", "", (recruiter_name or "").lower().strip())
    parts = [p for p in name.split() if p]
    if len(parts) >= 2 and domain:
        first, last = parts[0], parts[-1]
        out += [f"{first}.{last}@{domain}", f"{first}{last}@{domain}",
                f"{first[0]}{last}@{domain}", f"{first}@{domain}"]
    elif len(parts) == 1 and domain:
        out.append(f"{parts[0]}@{domain}")
    if domain:
        for g in GENERIC_INBOXES:
            out.append(f"{g}@{domain}")
    seen, dedup = set(), []
    for e in out:
        if e in seen or not EMAIL_RE.match(e):
            continue
        seen.add(e); dedup.append(e)
    return dedup


def anymail_finder_lookup(api_key: str, full_name: str, domain: str) -> tuple[str, str]:
    if not api_key or not full_name or not domain:
        return "", ""
    try:
        r = requests.post(
            "https://api.anymailfinder.com/v5.0/search/person.json",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"full_name": full_name, "domain": domain}, timeout=20,
        )
        if r.status_code >= 400:
            log.warning("AnyMailFinder %s: %s", r.status_code, r.text[:200])
            return "", ""
        j = r.json()
        if j.get("success") and j.get("results", {}).get("email"):
            res = j["results"]
            return res["email"], res.get("validation", "verified")
    except Exception as e:
        log.warning("AnyMailFinder error: %s", e)
    return "", ""


# ---------------------------------------------------------------------------
# ONE combined Grok call: JD intel + resume edits + cold email
# ---------------------------------------------------------------------------

COMBINED_SYSTEM = (
    "You handle THREE tasks for a single job application in one response. "
    "Work strictly from the JD text and candidate info provided - do not "
    "claim to access external sources.\n\n"
    "TASK 1 - JD intel. Extract from the JD content: company, title, "
    "location, work_mode, must_have_skills (5-10), nice_to_have_skills, "
    "responsibilities, companyDomain (your best inference from the JD "
    "URL and company name - e.g. acme.com), recruiterName (only if the "
    "JD names a specific recruiter/hiring manager; otherwise ''), "
    "recruiterTitle, recruiterLinkedIn, careersEmail (only if the JD "
    "publishes one like jobs@/careers@/talent@; otherwise ''), "
    "required_experience_years (a number: the MINIMUM years of "
    "professional experience the JD explicitly requires. If the JD says "
    "'3+ years' return 3; '5-7 years' return 5; '0-2 years' return 0; "
    "'no experience required' return 0; if the JD does not mention "
    "specific years, infer from the title - 'senior/staff/principal/"
    "lead' => 5, 'manager/director' => 7, 'intern/junior/entry' => 0, "
    "otherwise => 2), seniority_level ('intern' | 'junior' | 'mid' | "
    "'senior' | 'staff+' | 'manager+'), jd_summary (<=120 words).\n\n"
    "TASK 2 - Resume content edits. You'll get an array of resume lines "
    "with {id, text, max_chars}. Return ONLY lines whose text should be "
    "rephrased to better target the JD. RULES:\n"
    "  - Never exceed max_chars for any line (must fit original bbox).\n"
    "  - Never edit: names, emails, phone numbers, URLs, dates, company "
    "    names, school names, section headers (SKILLS, EXPERIENCE, "
    "    EDUCATION...), or anything that looks like an identifier.\n"
    "  - Never fabricate skills, employers, dates, or credentials.\n"
    "  - Mirror the JD's must-have keywords WHERE the candidate's "
    "    existing experience legitimately supports them.\n\n"
    "TASK 3 - Cold email. Write a tight, specific, human-sounding outreach "
    "email - NOT a template cover letter. Output:\n"
    "  - subject (<=70 chars, no clickbait, no emojis, no 'Application for "
    "    [role]' phrasing - make it specific, e.g. mention one project or "
    "    skill that ties to the role)\n"
    "  - greeting ('Hi <firstname>,' when recruiterName is set; else "
    "    'Hi <company> team,')\n"
    "  - body (90-140 words, plain text, 3 short paragraphs, no markdown, "
    "    no signature, no candidate name)\n\n"
    "REQUIRED body structure:\n"
    "  P1 INTRO (1-2 sentences): State WHO the candidate is in concrete "
    "    terms drawn directly from their resume - current role/title, "
    "    employer or school, and ONE specific anchor (a project name, a "
    "    tech they built, or a degree). Then note seeing the role.\n"
    "    Do NOT open with 'I'm excited to apply' / 'I'm reaching out' / "
    "    'I came across' - those are filler. Open with the candidate.\n"
    "  P2 WHY THEY FIT (2-3 sentences): Map exactly TWO concrete things "
    "    from the resume text to TWO must-have skills from the JD. Each "
    "    should be a SPECIFIC accomplishment - project/employer name + a "
    "    number or metric if the resume provides one - followed by which "
    "    JD requirement it speaks to. Show; don't tell.\n"
    "  P3 ASK (1 sentence): Direct request for a 15-min chat this week or "
    "    next.\n\n"
    "BANNED PHRASES (do not use any of these or close paraphrases):\n"
    "  'I'm excited to apply', 'I'm reaching out', 'I came across', "
    "  'My experience aligns', 'My background aligns', 'I have a strong "
    "  background in', 'I believe I can contribute', 'I'd love to discuss', "
    "  'perfect fit', 'passionate about', 'great fit', 'synergy', "
    "  'innovative solutions', 'practical expertise', 'hands-on experience "
    "  in', 'as advertised'.\n\n"
    "Use the candidate's actual project names, employer names, and "
    "technologies from their resume text. Never claim skills or "
    "accomplishments not in the resume.\n\n"
    "CRITICAL - NUMBERS RULE:\n"
    "  The user message includes a list called 'Allowed numbers from "
    "  resume'. The ONLY numeric values (percentages, multipliers, dollar "
    "  amounts, year counts, request volumes, etc.) you may put in the "
    "  email body or subject are values from that list, written CHARACTER-"
    "  FOR-CHARACTER as they appear. Do NOT round, inflate, deflate, or "
    "  approximate. Do NOT invent metrics. If the candidate's resume says "
    "  '8%', you write '8%' (never '~10%', '40%', 'significant', "
    "  'sizeable', or any other paraphrase). If no allowed number "
    "  legitimately supports the point you want to make, MAKE THE POINT "
    "  WITHOUT A NUMBER. Numbers in the JD (years required, salary, "
    "  company stats) are NOT allowed in the email body unless they also "
    "  appear in the candidate's resume.\n\n"
    "Output STRICT JSON with EXACTLY this shape:\n"
    "{\n"
    '  "jd": { company, title, location, work_mode, must_have_skills, '
    "nice_to_have_skills, responsibilities, companyDomain, recruiterName, "
    "recruiterTitle, recruiterLinkedIn, careersEmail, jd_summary },\n"
    '  "resume_edits": [ {"id": <int>, "text": "..."}, ... ],\n'
    '  "cold_email": { "subject": "...", "greeting": "...", "body": "..." }\n'
    "}\n"
    "Return ONLY the JSON, nothing else."
)


def analyze_job(grok: Grok, cfg: Config, job: dict, jd_html: str,
                resume_lines_payload: list[dict]) -> dict:
    jd_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", jd_html or ""))[:12000]
    allowed_nums = extract_resume_numbers(cfg.base_resume)
    user = (
        f"JD URL: {job.get('url', '')}\n"
        f"Company hint: {job.get('company', '')}\n\n"
        f"JD content:\n{jd_text}\n\n"
        f"Candidate:\n"
        f"  Name: {cfg.candidate['name']}\n"
        f"  Email: {cfg.candidate['email']}\n"
        f"  Phone: {cfg.candidate['phone']}\n"
        f"  LinkedIn: {cfg.candidate['linkedin']}\n\n"
        f"Candidate's resume (text extracted from PDF - only reference what's "
        f"actually here; never invent skills):\n{cfg.base_resume[:6500]}\n\n"
        f"Allowed numbers from resume (the ONLY numeric values you may cite "
        f"in the email - character-for-character, no rounding, no inflation; "
        f"if this list is empty, write the email with NO numbers at all):\n"
        f"{json.dumps(allowed_nums, ensure_ascii=False)}\n\n"
        f"Resume lines for Task 2:\n"
        f"{json.dumps(resume_lines_payload, ensure_ascii=False)}"
    )
    return grok.chat_json(COMBINED_SYSTEM, user, timeout=180)


# ---------------------------------------------------------------------------
# Email body assembly (cold pitch only - resume goes as PDF attachment)
# ---------------------------------------------------------------------------

def assemble_email_html(pitch: dict, cfg: Config) -> str:
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body_paragraphs = "".join(
        f'<p style="margin:0 0 12px 0;">{esc(p).replace(chr(10), "<br/>")}</p>'
        for p in re.split(r"\n{2,}", pitch.get("body", ""))
    )
    return (
        f'<div style="font-family:Helvetica,Arial,sans-serif;color:#111;'
        f'font-size:14px;line-height:1.55;max-width:640px;">'
        f'<p style="margin:0 0 12px 0;">{esc(pitch.get("greeting", "Hi there,"))}</p>'
        f'{body_paragraphs}'
        f'<p style="margin:16px 0 4px 0;">Best,<br/>{esc(cfg.candidate["name"])}</p>'
        f'<p style="margin:0;color:#555;font-size:12px;">'
        f'{esc(cfg.candidate["email"])} &middot; {esc(cfg.candidate["phone"])} &middot; '
        f'<a href="{esc(cfg.candidate["linkedin"])}">LinkedIn</a></p>'
        f'<p style="margin:14px 0 0 0;color:#555;font-size:12px;">Resume attached.</p>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Gmail sender
# ---------------------------------------------------------------------------

class Gmailer:
    def __init__(self, address: str, app_password: str, send: bool = True):
        self.address = address
        self.app_password = app_password
        self.send_enabled = send

    @property
    def is_configured(self) -> bool:
        return bool(self.app_password and self.app_password.strip())

    def send(self, to: str, subject: str, html_body: str,
             attachments: list[Path] | None = None) -> bool:
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content("This email contains HTML. Please view in an HTML-capable client.")
        msg.add_alternative(html_body, subtype="html")
        for p in attachments or []:
            p = Path(p)
            mime, _ = mimetypes.guess_type(p.name)
            mtype, stype = (mime or "application/octet-stream").split("/", 1)
            with open(p, "rb") as f:
                msg.add_attachment(f.read(), maintype=mtype, subtype=stype, filename=p.name)
        if not self.send_enabled:
            log.info("DRY RUN - would send to %s subject=%r (attachments=%s)",
                     to, subject, [a.name for a in (attachments or [])])
            return True
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(self.address, self.app_password)
            s.send_message(msg)
        return True


# ---------------------------------------------------------------------------
# Per-job pipeline (one Grok call total)
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    job_id: str
    status: str
    sent_to: str = ""
    subject: str = ""
    pdf_path: str = ""
    note: str = ""
    jd_summary: str = ""


def apply_to_job(job: dict, cfg: Config, grok: Grok, mailer: Gmailer,
                 db: DB) -> ApplyResult:
    job_id = job["id"]
    title = job.get("title", "")
    source = job.get("source", "")
    log.info("=== Applying: %s @ %s (%s)",
             title, job.get("company"), job["url"])

    # 0a. Cheap pre-LLM filter: skip clearly-senior titles before spending a Grok call.
    blocklist = cfg.filters.get("block_title_keywords", []) or []
    if title_is_blocked(title, blocklist):
        note = f"title blocked by filter: {title!r}"
        db.mark(job_id, "skipped", note)
        return ApplyResult(job_id, "skipped",
                           note=note)

    # 0b. Location pre-filter on stored location field.
    if cfg.filters.get("block_non_us", True):
        _loc_blocked, _loc_reason = location_blocked(job.get("location", ""))
        if _loc_blocked:
            db.mark(job_id, "skipped", _loc_reason)
            return ApplyResult(job_id, "skipped", note=_loc_reason)

    # 0c. Visa / sponsorship / clearance pre-filter on title + cached description.
    if cfg.filters.get("block_no_sponsorship", True):
        _v_blocked, _v_reason = visa_sponsorship_blocked(title, job.get("description", ""))
        if _v_blocked:
            db.mark(job_id, "skipped", _v_reason)
            return ApplyResult(job_id, "skipped", note=_v_reason)

    max_years = configured_max_years(cfg.filters)
    _exp_blocked, _exp_reason = experience_requirement_blocked(
        f"{title} {job.get('description', '')}", max_years)
    if _exp_blocked:
        db.mark(job_id, "skipped", _exp_reason)
        return ApplyResult(job_id, "skipped", note=_exp_reason)

    if cfg.filters.get("block_phd_required", True):
        _phd_blocked, _phd_reason = phd_required_blocked(
            f"{title} {job.get('description', '')}")
        if _phd_blocked:
            db.mark(job_id, "skipped", _phd_reason)
            return ApplyResult(job_id, "skipped", note=_phd_reason)

    # 1. Fetch JD page; fall back to cached description if fetch fails or returns thin HTML.
    html = ""
    try:
        html = requests.get(job["url"], timeout=25, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; job-autopilot)"
        }).text
    except Exception as e:
        log.warning("JD fetch failed for %s (%s); using cached description", job_id, e)

    jd_plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))
    if len(jd_plain.strip()) < 300 and job.get("description"):
        log.debug("JD page thin for %s (%d chars); using cached description for scoring",
                  job_id, len(jd_plain.strip()))
        jd_plain = job["description"]

    # 1b. Repost check on full JD text.
    if "repost" in jd_plain[:2000].lower() or job.get("reposted", False):
        note = "reposted job"
        db.mark(job_id, "skipped", note)
        return ApplyResult(job_id, "skipped", note=note)

    # 1c. Visa / sponsorship / clearance filter on full fetched JD text.
    if cfg.filters.get("block_no_sponsorship", True):
        _v_blocked, _v_reason = visa_sponsorship_blocked("", jd_plain[:6000])
        if _v_blocked:
            db.mark(job_id, "skipped", _v_reason)
            return ApplyResult(job_id, "skipped", note=_v_reason)

    _exp_blocked, _exp_reason = experience_requirement_blocked(jd_plain[:8000], max_years)
    if _exp_blocked:
        db.mark(job_id, "skipped", _exp_reason)
        return ApplyResult(job_id, "skipped", note=_exp_reason)

    if cfg.filters.get("block_phd_required", True):
        _phd_blocked, _phd_reason = phd_required_blocked(jd_plain[:8000])
        if _phd_blocked:
            db.mark(job_id, "skipped", _phd_reason)
            return ApplyResult(job_id, "skipped", note=_phd_reason)

    # 1d. Re-score using full JD text (snippet at search time was incomplete).
    # If the job was already LLM-scored during scouting (fit_summary present),
    # skip the expensive re-score; otherwise run LLM scoring now with the full JD.
    old_score = int(job.get("match_score") or 0)
    already_llm_scored = bool(job.get("fit_summary", ""))
    if not already_llm_scored:
        full_job = {**job, "description": jd_plain[:12000]}
        llm_result = llm_score_resume_match(grok, cfg.base_resume, cfg.roles, full_job)
        if llm_result is not None:
            full_score = llm_result["score"]
            fit_summary = llm_result["fit_summary"]
        else:
            full_score = resume_relevance_score(cfg.base_resume, cfg.roles, full_job)
            fit_summary = ""
        if full_score != old_score or fit_summary:
            db.conn.execute(
                "UPDATE jobs SET match_score=?, fit_summary=? WHERE id=?",
                (full_score, fit_summary, job_id),
            )
            db.conn.commit()
            log.info("re-scored %s: old=%d llm=%d", job_id, old_score, full_score)
    else:
        full_score = old_score
    min_match = int(cfg.behavior.get("min_resume_match_score", 35) or 0)
    if min_match and full_score < min_match:
        note = f"score {full_score}/100 below minimum {min_match}"
        db.mark(job_id, "skipped", note)
        return ApplyResult(job_id, "skipped",
                           note=note)

    # 1e. Skip LLM entirely when email and resume tailoring are both off.
    tailor_enabled = bool(cfg.behavior.get("tailor_resume", False))
    needs_llm = (
        tailor_enabled or
        bool(cfg.auto_apply.get("enabled", False)) or
        (mailer.is_configured and bool(cfg.gmail.get("send", True)))
    )
    if not needs_llm:
        db.mark(job_id, "ready_for_review")
        return ApplyResult(job_id, "ready_for_review",
                           note=f"full-JD score={full_score} — queued for manual review")

    # 2. Extract resume lines from the PDF (no LLM) - only when tailoring.
    resume_lines_payload: list[dict] = []
    line_map: dict[int, dict] = {}
    if tailor_enabled:
        try:
            line_map, resume_lines_payload = extract_resume_lines(cfg.resume_pdf_path)
        except Exception as e:
            log.warning("resume-line extraction failed (%s); tailoring disabled for this job", e)
            tailor_enabled = False

    # 3. ONE Grok call: JD intel + resume edits + cold email.
    try:
        result = analyze_job(grok, cfg, job, html, resume_lines_payload)
    except Exception as e:
        return ApplyResult(job_id, "error", note=f"grok analyze failed: {e}")

    jd = result.get("jd") or {}
    jd.setdefault("company", job.get("company", ""))
    jd.setdefault("title", job.get("title", ""))
    domain = slug_domain(jd.get("companyDomain", "")) or slug_domain(jd.get("company", ""))
    jd["companyDomain"] = domain
    pitch = result.get("cold_email") or {}
    resume_edits = result.get("resume_edits") or []

    # 3a. Skip if the JD requires too many years OR if the LLM tagged the
    #     title as senior+/staff+/manager+.
    try:
        years = int(jd.get("required_experience_years", 0) or 0)
    except (TypeError, ValueError):
        years = 0
    seniority = (jd.get("seniority_level") or "").strip().lower()
    too_senior = seniority in {"senior", "staff+", "manager+"}
    if years > max_years or too_senior:
        note = (f"experience filter: years={years}>cap={max_years} "
                f"seniority={seniority!r}")
        db.mark(job_id, "skipped", note)
        return ApplyResult(
            job_id, "skipped",
            note=note,
            jd_summary=jd.get("jd_summary", ""),
        )

    # 4. Recruiter lookup.
    amf_email, _ = anymail_finder_lookup(cfg.amf_key, jd.get("recruiterName", ""), domain)
    cands = candidate_emails(jd.get("recruiterName", ""), domain, jd.get("careersEmail", ""))
    if amf_email:
        cands = [amf_email] + [c for c in cands if c != amf_email]

    # 5. Apply resume edits to a tailored PDF. Fall back to original on failure.
    attached_pdf = cfg.resume_pdf_path
    tailored_note = "original"
    if tailor_enabled and resume_edits:
        pdf_name = (f"{slug(cfg.candidate['name'])}_resume_"
                    f"{slug(jd.get('company', 'company'))}_"
                    f"{slug(jd.get('title', 'role'))}.pdf")
        tailored_path = cfg.output_dir / "resumes" / pdf_name
        try:
            apply_resume_edits(cfg.resume_pdf_path, line_map, resume_edits, tailored_path)
            attached_pdf = tailored_path
            tailored_note = "tailored"
            log.info("tailored PDF: %s (%d edits)", tailored_path, len(resume_edits))
        except Exception as e:
            log.warning("PDF tailoring failed (%s); attaching original", e)

    # 6. Hard guard: any numeric token in subject/body that doesn't appear in the
    # resume is a hallucination - refuse to send/apply rather than make a wrong claim.
    allowed_nums = extract_resume_numbers(cfg.base_resume)
    bad_nums = disallowed_pitch_numbers(pitch, allowed_nums)
    if bad_nums:
        log.warning("LLM emitted numbers not in resume: %s | allowed=%s",
                    bad_nums, allowed_nums)
        note = f"hallucinated numbers in email: {bad_nums}"
        db.mark(job_id, "skipped", note)
        return ApplyResult(
            job_id, "skipped",
            note=note,
        )

    # 7. Browser apply path: company sites / Easy Apply. Defaults to a filled
    #    form left ready for human review unless auto_apply.submit=true.
    if cfg.auto_apply.get("enabled", False):
        try:
            from applicator import apply_with_browser
            browser_result = apply_with_browser(job, cfg, jd, pitch, attached_pdf)
        except Exception as e:
            browser_result = None
            log.warning("browser apply failed before launch: %s", e)

        if browser_result and browser_result.status in {"submitted", "ready_for_review"}:
            final_status = "applied" if browser_result.status == "submitted" else "ready_for_review"
            if cfg.behavior.get("auto_watch_companies", True) and domain:
                db.watch(domain, jd.get("company", ""), careers_url=f"https://{domain}/careers")
            db.mark(job_id, final_status)
            db.log_application(
                job_id,
                jd.get("recruiterName", ""),
                browser_result.destination,
                f"{browser_result.method}:{browser_result.status}",
                str(attached_pdf),
                notes=(f"resume={tailored_note} screenshot={browser_result.screenshot_path} "
                       f"note={browser_result.note}"),
            )
            return ApplyResult(
                job_id,
                final_status,
                sent_to=browser_result.destination,
                subject=f"{browser_result.method}:{browser_result.status}",
                pdf_path=str(attached_pdf),
                note=browser_result.note,
                jd_summary=jd.get("jd_summary", ""),
            )

        fail_note = (
            browser_result.note
            if browser_result
            else "browser apply unavailable"
        )
        if not cfg.auto_apply.get("email_fallback", True):
            db.mark(job_id, "ready_for_review")
            return ApplyResult(job_id, "ready_for_review", note=fail_note)
        # If Gmail is not configured, skip email and park job for manual review.
        if not mailer.is_configured:
            log.info("browser apply did not complete (%s); Gmail not configured — "
                     "parking as ready_for_review", fail_note)
            db.mark(job_id, "ready_for_review")
            return ApplyResult(job_id, "ready_for_review",
                               note=f"{fail_note} | run: autopilot.py login",
                               jd_summary=jd.get("jd_summary", ""))
        log.info("browser apply did not complete (%s); falling back to email", fail_note)

    # 8. Send recruiter/careers email fallback.
    if not mailer.is_configured:
        db.mark(job_id, "ready_for_review")
        return ApplyResult(job_id, "ready_for_review",
                           note="Gmail not configured — set gmail.app_password in config.yaml",
                           jd_summary=jd.get("jd_summary", ""))
    if not pitch.get("subject") or not pitch.get("body"):
        return ApplyResult(job_id, "error", note="cold email missing subject/body")
    if cfg.behavior.get("require_verified_email") and not amf_email:
        note = "no verified email (require_verified_email=true)"
        db.mark(job_id, "skipped", note)
        return ApplyResult(job_id, "skipped",
                           note=note)
    if not cands:
        note = "no recipient found"
        db.mark(job_id, "skipped", note)
        return ApplyResult(job_id, "skipped", note=note)
    primary = cands[0]

    email_html = assemble_email_html(pitch, cfg)
    try:
        mailer.send(primary, pitch["subject"], email_html, attachments=[attached_pdf])
    except Exception as e:
        return ApplyResult(job_id, "error", note=f"send failed: {e}")

    # 9. Auto-watch the company.
    if cfg.behavior.get("auto_watch_companies", True) and domain:
        db.watch(domain, jd.get("company", ""), careers_url=f"https://{domain}/careers")

    # 10. Log.
    db.mark(job_id, "applied")
    db.log_application(job_id, jd.get("recruiterName", ""), primary,
                       pitch["subject"], str(attached_pdf),
                       notes=f"resume={tailored_note} amf={'y' if amf_email else 'n'} "
                             f"candidates={','.join(cands[:5])}")
    return ApplyResult(job_id, "applied", sent_to=primary,
                       subject=pitch["subject"], pdf_path=str(attached_pdf),
                       jd_summary=jd.get("jd_summary", ""))


def setup_logging(output_dir: Path, verbose: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(output_dir / "logs" / "autopilot.log"),
        ],
    )
