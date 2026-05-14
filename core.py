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
    gmail: dict
    amf_key: str
    behavior: dict
    filters: dict
    auto_apply: dict
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

        filters_cfg = dict(d.get("filters") or {})
        filters_cfg.setdefault("block_title_keywords", DEFAULT_BLOCK_TITLES)
        filters_cfg.setdefault("max_years_required", 2)
        filters_cfg.setdefault("block_no_sponsorship", True)
        filters_cfg.setdefault("block_staffing_agencies", True)
        filters_cfg.setdefault("block_companies", [])

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
            gmail=d["gmail"],
            amf_key=(d.get("anymail_finder", {}) or {}).get("api_key", "") or "",
            behavior=d.get("behavior", {}) or {},
            filters=filters_cfg,
            auto_apply=d.get("auto_apply", {}) or {},
            raw=d,
        )


def title_is_blocked(title: str, blocklist: list[str]) -> bool:
    """True if the (lowercased) title contains any blocked keyword as a substring."""
    if not title:
        return False
    t = " " + title.lower() + " "
    return any(kw.lower() in t for kw in (blocklist or []))


# Substring keywords that identify staffing/recruiting company names.
# Checked against the lowercased company name (no word-boundary restriction so
# camelCase names like "TechStaffing" are caught too).
_STAFFING_COMPANY_KEYWORDS: tuple[str, ...] = (
    "staffing", "recruiting", "recruitment",
    "talent solutions", "talent acquisition",
    "workforce solutions", "workforce management",
    "it staffing", "tech staffing",
    "placement services", "placement firm", "placement group",
    "insight global", "beaconfire", "beacon fire",
    "dice",
)

# Signals in the job description that the poster is a middleman.
# Only patterns that are essentially NEVER used by real direct employers:
#   - "on behalf of our/a client"            → 100% staffing
#   - "our client is looking/seeking/hiring" → 100% staffing
#   - "our client, a [company]"             → 100% staffing
#   - "we are a staffing [firm/agency/…]"   → 100% staffing
#   - "staffing agency/firm/company"        → 100% staffing
#   - "third-party contractor/staffing"     → 100% staffing
#
# Intentionally NOT included (real companies use these legitimately):
#   "contract to hire", "c2c", "corp-to-corp" — employment TYPE, not middleman signal.
_STAFFING_DESC_RE = re.compile(
    r'(?:'
    r'\bon\s+behalf\s+of\s+(?:our|a)\s+client\b'
    r'|\bour\s+client(?:,|\s+is\s+(?:looking|seeking|searching|hiring)|\s+(?:seeks?|requires?|needs?)\s+(?:a|an)\b)'
    r'|\bour\s+(?:premier|valued|top|key|exclusive)\s+client\b'
    r'|\bhiring\s+for\s+(?:a|our)\s+client\b'
    r'|\bwe\s+are\s+a\s+(?:leading\s+|top\s+)?staffing\b'
    r'|\bstaffing\s+(?:agency|firm|company|provider)\b'
    r'|\bthird[\s\-]party\s+(?:contract(?:or)?|staffing|recruiter|placement)\b'
    r')',
    re.IGNORECASE,
)


def is_staffing_or_agency(company: str, description: str) -> tuple[bool, str]:
    """Return (True, reason) if the job is from a staffing firm or third-party
    contractor posting on behalf of an actual employer."""
    if company:
        cl = company.lower()
        for kw in _STAFFING_COMPANY_KEYWORDS:
            if kw in cl:
                return True, f"staffing/agency filter: company name {company!r}"
    text = f"{company or ''} {description or ''}"
    m = _STAFFING_DESC_RE.search(text)
    if m:
        return True, f"staffing/agency filter: {m.group(0)!r}"
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
    # US Citizen / GC only
    r'|\b(?:us|u\.s\.)\s+citizens?\s+only\b'
    r'|\bonly\s+(?:us|u\.s\.)\s+citizens?\b'
    r'|\bmust\s+be\s+a?\s*(?:us|u\.s\.)\s+citizen\b'
    r'|\bauthorized\s+to\s+work\b.{0,60}without\s+(?:visa\s+)?sponsorship\b'
    r'|\bwork\s+without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship\b'
    r'|\b(?:gc|green\s+card)\s+or\s+(?:us\s+)?citizen\s+(?:only|required)\b'
    r'|\bcitizen\s+or\s+(?:gc|green\s+card)\s+(?:only|required)\b'
    r'|\bcitizenship\s+required\b'
    r'|\bpermanent\s+resident\s+or\s+(?:us\s+)?citizen\s+(?:only|required)\b'
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


def configured_max_years(filters: dict) -> int:
    raw = (filters or {}).get("max_years_required", 2)
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

    With max_years=2, this blocks "3+ years", "minimum 3 years", "4 years
    experience", "3-5 years", etc. Ranges use the lower bound, so "0-2" and
    "1-3" remain eligible because the required minimum is entry-level.
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
    # robotics / quant targets
    "robotics", "robotic", "robot", "ros", "ros2", "autonomous", "autonomy",
    "perception", "controls", "control", "planning", "slam", "lidar",
    "quant", "quantitative", "trading", "alpha", "portfolio", "stochastic",
    "statistics", "optimization", "time-series",
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
    # Tier 1
    "rag": 5, "llm": 5, "langchain": 5, "agentic": 5, "rlhf": 5,
    "transformers": 5, "pytorch": 5, "qlora": 5, "lora": 5,
    "pgvector": 5, "knowledge graph": 5, "hugging face": 5,
    "huggingface": 5, "fine-tuning": 5, "finetuning": 5,
    "llama": 5, "gpt-4": 5, "generative ai": 5, "llm agent": 5,
    # Tier 2
    "tensorflow": 3, "xgboost": 3, "scikit-learn": 3, "sklearn": 3,
    "bayesian": 3, "fastapi": 3, "docker": 3, "kubernetes": 3,
    "aws": 3, "postgresql": 3, "nlp": 3, "embeddings": 3,
    "mlops": 3, "streamlit": 3, "gpt": 3, "bert": 3,
    "machine learning": 3, "deep learning": 3, "data science": 3,
    "reinforcement learning": 3, "openai": 3, "anthropic": 3,
    "vector database": 3, "model fine": 3, "feature engineering": 3,
    "robotics": 3, "robotic": 3, "autonomous": 3, "perception": 3,
    "controls": 3, "optimization": 3, "quantitative": 3, "quant": 3,
    "stochastic": 3, "time series": 3,
    # Tier 3
    "ai": 2, "ml": 2,
    "python": 1, "sql": 1, "git": 1, "github actions": 1, "ci/cd": 1,
    "agents": 1, "inference": 1, "model deployment": 1, "s3": 1,
    "lambda": 1, "data pipeline": 1, "anomaly detection": 1,
}

# Strong-match denominator: sum of the strongest few resume-skill signals.
# This avoids the old "must match nearly the whole resume skill list" behavior.
_STRONG_SKILL_HIT_TARGET = float(sum(sorted(_RESUME_SKILL_WEIGHTS.values(), reverse=True)[:4]))

# ML/AI presence check — if NONE of these appear in a job, it is probably
# unrelated to your background and gets a score penalty.
_ML_PRESENCE_TERMS = re.compile(
    r"\b(machine learning|deep learning|artificial intelligence|"
    r"llm|llms|nlp|rag|ai|ml|ai/ml|ml/ai|neural|generative|data science|data scientist|"
    r"computer vision|reinforcement|foundation model|"
    r"predictive model|model deployment|forecasting|"
    r"robotics|robotic|autonomous systems|perception|controls|"
    r"quant|quantitative|algorithmic trading|alpha research)\b",
    re.I,
)

_NON_TECH_ROLE_RE = re.compile(
    r"\b(content|writer|copywriter|marketing|sales|business analyst|"
    r"operations specialist|customer support|account executive|"
    r"product owner|program manager|project manager)\b",
    re.I,
)

_ROLE_SPECIFIC_TERMS = {
    "ai", "ml", "llm", "llms", "rag", "nlp", "agentic", "generative",
    "machine", "learning", "data", "scientist", "science", "applied",
    "research", "model", "models", "vision", "inference", "deployment",
    "robotics", "robotic", "robot", "autonomous", "autonomy", "perception",
    "controls", "quant", "quantitative", "analyst", "finance", "trading",
    "alpha", "portfolio", "stochastic", "optimization",
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
    },
    "llm_generative": {
        "llm", "llms", "gpt", "llama", "mistral", "generative", "rag",
        "transformers", "embeddings", "vector", "pgvector", "langchain",
        "langgraph", "agentic", "agents", "fine-tuning", "finetuning",
        "lora", "qlora", "prompt", "retrieval",
    },
    "ml_modeling": {
        "pytorch", "tensorflow", "sklearn", "scikit", "xgboost",
        "lightgbm", "catboost", "bayesian", "reinforcement", "nlp",
        "multimodal", "diffusion", "feature", "features", "training",
        "inference", "evaluation", "deployment",
    },
    "data_science": {
        "data", "scientist", "science", "analytics", "pandas", "numpy",
        "scipy", "sql", "statistics", "statistical", "experimentation",
        "ab", "dashboard", "tableau", "powerbi", "pipeline", "etl",
    },
    "robotics_ai": {
        "robotics", "robotic", "robot", "ros", "ros2", "autonomous",
        "autonomy", "perception", "controls", "control", "planning",
        "slam", "lidar", "computer", "vision", "navigation", "sensor",
    },
    "quant_finance": {
        "quant", "quantitative", "trading", "alpha", "portfolio",
        "stochastic", "optimization", "finance", "financial", "market",
        "markets", "time-series", "timeseries", "risk", "pricing",
        "researcher", "analyst",
    },
    "engineering_platform": {
        "python", "fastapi", "docker", "kubernetes", "aws", "azure",
        "postgres", "postgresql", "redis", "celery", "api", "apis",
        "backend", "distributed", "cloud", "mlops", "ci/cd", "s3",
        "lambda", "gpu", "cuda", "vllm",
    },
}

_CONCEPT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "genai": ("generative", "ai"),
    "retrieval augmented generation": ("rag",),
    "large language model": ("llm",),
    "large language models": ("llms", "llm"),
    "natural language processing": ("nlp",),
    "computer vision": ("vision", "perception"),
    "autonomous vehicles": ("autonomous", "robotics"),
    "quant researcher": ("quant", "researcher"),
    "quant analyst": ("quant", "analyst"),
    "quant engineer": ("quant", "engineering"),
    "time series": ("time-series", "timeseries"),
}


def _skill_hit_score(title: str, desc: str) -> float:
    """0.0–1.0: fraction of your resume skills (by weight) found in the job."""
    title_l = title.lower()
    desc_l  = desc.lower()
    hits = 0.0
    for skill, w in _RESUME_SKILL_WEIGHTS.items():
        pat = r"\b" + re.escape(skill) + r"\b"
        # Title hit counts 1.5× — titles are always present, descriptions often aren't.
        if re.search(pat, title_l):
            hits += w * 1.5
        elif re.search(pat, desc_l):
            hits += w
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
        if role_kw.lower() in title_l:
            component = max(component, 1.0)
        best = max(best, component)
    return min(1.0, best)


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

    raw = (
        0.25 * role_component +
        0.25 * skill_score +
        0.20 * semantic_component +
        0.15 * ontology_component +
        0.10 * resume_component +
        0.05 * title_component
    )

    caps: list[str] = []
    if not ml_intent:
        raw = min(raw * 0.45, 0.28)
        caps.append("no AI/ML/robotics/quant intent")
    elif skill_score < 0.10 and role_component < 0.70:
        raw = min(raw, 0.34)
        caps.append("weak target-role and resume-skill evidence")
    if _NON_TECH_ROLE_RE.search(title_lower):
        raw = min(raw, 0.34)
        caps.append("non-technical AI-adjacent title family")

    score = int(round(100.0 * min(1.0, raw)))
    if score >= 75:
        tier = "strong"
    elif score >= 55:
        tier = "review"
    elif score >= 35:
        tier = "weak_review"
    else:
        tier = "reject"

    return {
        "score": score,
        "scorecard_points": round(25.0 * role_component, 1),
        "resume_skill_points": round(25.0 * skill_score, 1),
        "semantic_points": round(20.0 * semantic_component, 1),
        "ontology_points": round(15.0 * ontology_component, 1),
        "technical_overlap_points": round(10.0 * resume_component, 1),
        "title_bonus_points": round(5.0 * title_component, 1),
        "tier": tier,
        "caps": caps,
    }


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
    first_seen   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new'
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
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        jobs_cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "match_score" not in jobs_cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN match_score INTEGER DEFAULT 0")
        if "skip_reason" not in jobs_cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN skip_reason TEXT")
        company_cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "careers_unreachable" not in company_cols:
            self.conn.execute(
                "ALTER TABLE companies ADD COLUMN careers_unreachable INTEGER DEFAULT 0"
            )

    def seen(self, job_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None

    def insert_job(self, job: dict) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (id, source, url, title, company, location, description, posted_at, match_score, skip_reason, first_seen, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], job["source"], job["url"], job.get("title", ""),
             job.get("company", ""), job.get("location", ""),
             (job.get("description") or "")[:8000],
             job.get("posted_at", ""), int(job.get("match_score", 0) or 0),
             job.get("skip_reason", ""),
             datetime.now(timezone.utc).isoformat(), "new"),
        )
        self.conn.commit()

    def queue_or_refresh_job(self, job: dict) -> None:
        """Insert a reviewable job, or revive an older low-score skipped row.

        We intentionally do not revive jobs skipped by title, visa, repost, or
        experience filters. This only rescues rows whose old skip reason was a
        low/unknown ATS score before richer detail text was available.
        """
        if not self.seen(job["id"]):
            self.insert_job(job)
            return
        self.conn.execute(
            """UPDATE jobs
               SET match_score=?, description=?, status='new', skip_reason=''
               WHERE id=?
                 AND status='skipped'
                 AND (
                     skip_reason IS NULL
                     OR trim(skip_reason)=''
                     OR skip_reason LIKE 'ATS score %'
                     OR skip_reason='historical skipped before reason tracking'
                 )""",
            (
                int(job.get("match_score", 0) or 0),
                (job.get("description") or "")[:8000],
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
            "skipped":    c("SELECT COUNT(*) FROM jobs WHERE status='skipped'").fetchone()[0],
            "ready_for_review": c("SELECT COUNT(*) FROM jobs WHERE status='ready_for_review'").fetchone()[0],
            "error":      c("SELECT COUNT(*) FROM jobs WHERE status='error'").fetchone()[0],
            "new":        c("SELECT COUNT(*) FROM jobs WHERE status='new'").fetchone()[0],
            "watched_companies": c("SELECT COUNT(*) FROM companies").fetchone()[0],
        }

    def export_excel(self, path: Path) -> None:
        """Write (or overwrite) an Excel workbook at *path*.

        Sheet layout
        ────────────
        • Sheet 1  "Summary"      — overall totals + one row per day
        • Sheet 2+ "YYYY-MM-DD"   — every job found on that date (newest date first)
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
            "applied":          "FF90EE90",
            "ready_for_review": "FFFFFF99",
            "new":              "FFCCE5FF",
            "skipped":          "FFD3D3D3",
            "error":            "FFFFCCCC",
        }
        STATUSES = ["applied", "ready_for_review", "new", "skipped", "error"]

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

        def write_job_row(ws, row_num, r):
            status = r.get("status", "new") or "new"
            fill   = PatternFill("solid", fgColor=STATUS_COLORS.get(status, "FFFFFFFF"))
            values = [
                status,
                r.get("match_score") or 0,
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
            ]
            for c, val in enumerate(values, 1):
                cell = ws.cell(row=row_num, column=c, value=val or "")
                cell.fill  = fill
                cell.alignment = TOP
                if c == 9 and val:
                    cell.hyperlink = val
                    cell.font = Font(color="FF0563C1", underline="single")

        # ── fetch all rows with date tag ──────────────────────────────────────
        all_rows = self.conn.execute(
            """
            SELECT
                j.status, j.match_score, j.title, j.company, j.location,
                j.source, j.posted_at, j.first_seen, j.url, j.skip_reason,
                a.sent_to, a.sent_at, a.subject AS method, a.recruiter, a.notes,
                substr(j.first_seen, 1, 10) AS found_date
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            ORDER BY j.first_seen DESC, j.match_score DESC
            """
        ).fetchall()

        # Group by date
        by_date: dict[str, list[dict]] = defaultdict(list)
        for row in all_rows:
            r = dict(row)
            by_date[r.get("found_date", "unknown")].append(r)

        # ── workbook ─────────────────────────────────────────────────────────
        wb = openpyxl.Workbook()

        # ════════════════════════════════════════════════════════════════════
        # Sheet 1 — Summary
        # ════════════════════════════════════════════════════════════════════
        ws_sum = wb.active
        ws_sum.title = "Summary"

        # Overall totals block (rows 1-3)
        total_stats = self.stats()
        TILE_LABELS = ["Total Found", "Applied", "Ready for Review", "New", "Skipped", "Error"]
        TILE_KEYS   = ["total_jobs", "applied", "ready_for_review", "new", "skipped", "error"]
        TILE_COLORS = ["FF2F5496", "FF90EE90", "FFFFFF99", "FFCCE5FF", "FFD3D3D3", "FFFFCCCC"]
        TILE_FONT_C = ["FFFFFFFF", "FF000000", "FF000000", "FF000000", "FF000000", "FF000000"]

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
        DAY_HEADERS  = ["Date", "Total Found", "Applied", "Ready for Review", "New", "Skipped", "Error"]
        DAY_WIDTHS   = [14, 13, 10, 18, 8, 10, 8]
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
            row_vals  = [date, len(jobs),
                         counts["applied"], counts["ready_for_review"],
                         counts["new"], counts["skipped"], counts["error"]]
            for c, val in enumerate(row_vals, 1):
                cell = ws_sum.cell(row=r_num, column=c, value=val)
                cell.alignment = CENTER
                if c == 1:
                    cell.font = Font(bold=True)

        ws_sum.freeze_panes = "A5"

        # ════════════════════════════════════════════════════════════════════
        # One sheet per day
        # ════════════════════════════════════════════════════════════════════
        JOB_HEADERS = [
            "Status", "Match %", "Title", "Company", "Location",
            "Source", "Posted At", "Found At", "Apply URL",
            "Skip Reason", "Sent To", "Applied At", "Method", "Recruiter", "Notes",
        ]
        JOB_WIDTHS = [16, 9, 36, 28, 22, 10, 20, 20, 50, 42, 36, 20, 22, 22, 40]

        for date in sorted(by_date.keys(), reverse=True):
            ws = wb.create_sheet(title=date)
            style_header_row(ws, JOB_HEADERS, JOB_WIDTHS)
            jobs = sorted(
                by_date[date],
                key=lambda j: (
                    STATUSES.index(j["status"]) if j["status"] in STATUSES else 99,
                    -(j.get("match_score") or 0),
                ),
            )
            for row_num, r in enumerate(jobs, 2):
                write_job_row(ws, row_num, r)

        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(path))
        log.info("Excel export updated: %s  (%d rows, %d daily sheets)",
                 path, len(all_rows), len(by_date))


# ---------------------------------------------------------------------------
# Grok client
# ---------------------------------------------------------------------------

class Grok:
    """Talks to any OpenAI-compatible /chat/completions endpoint.
    Default is xAI Grok, but pass a different base_url to use OpenAI,
    Groq, DeepSeek, Together, Gemini's OpenAI mode, OpenRouter, etc."""

    def __init__(self, api_key: str, model: str = "grok-4",
                 base_url: str = "https://api.x.ai/v1",
                 live_search: bool = False):
        self.api_key = api_key
        self.model = model
        # Tolerate base_url with or without /v1 suffix or trailing slash.
        bu = (base_url or "https://api.x.ai/v1").rstrip("/")
        if not bu.endswith("/v1") and "/v1" not in bu:
            bu = bu + "/v1"
        self.url = bu + "/chat/completions"
        self.live_search = live_search  # kept for forward-compat; unused

    def chat(self, system: str, user: str, json_mode: bool = False,
             timeout: int = 120, search: bool | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        # xAI retired Live Search 2026-01-12; we no longer send search_parameters.
        _ = search if search is not None else self.live_search
        r = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=body, timeout=timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"LLM error {r.status_code}: {r.text[:500]}")
        return r.json()["choices"][0]["message"]["content"].strip()

    def chat_json(self, system: str, user: str, **kw) -> dict:
        raw = self.chat(system, user, json_mode=True, **kw)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if m:
                return json.loads(m.group(0))
            raise


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
        f"actually here; never invent skills):\n{cfg.base_resume[:3500]}\n\n"
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

    # 0b. Visa / sponsorship / clearance pre-filter on title + cached description.
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

    # 1d. Re-score using full JD text (snippet at search time was incomplete).
    old_score = int(job.get("match_score") or 0)
    full_score = resume_relevance_score(cfg.base_resume, cfg.roles,
                                        {**job, "description": jd_plain[:12000]})
    if full_score != old_score:
        db.conn.execute("UPDATE jobs SET match_score=? WHERE id=?", (full_score, job_id))
        db.conn.commit()
        log.info("re-scored %s: snippet=%d full-jd=%d", job_id, old_score, full_score)
    min_match = int(cfg.behavior.get("min_resume_match_score", 35) or 0)
    if min_match and full_score < min_match:
        note = f"full-JD ATS score {full_score} below minimum {min_match}"
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
