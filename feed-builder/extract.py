"""Pull dates, places, pay, and level out of a public job posting."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi", "ia", "id",
    "il", "in", "ks", "ky", "la", "ma", "md", "me", "mi", "mn", "mo", "ms", "mt", "nc",
    "nd", "ne", "nh", "nj", "nm", "nv", "ny", "oh", "ok", "or", "pa", "ri", "sc", "sd",
    "tn", "tx", "ut", "va", "vt", "wa", "wi", "wv", "wy",
}
NON_US_REGIONS = {
    "on": "Canada", "bc": "Canada", "qc": "Canada", "ab": "Canada", "sk": "Canada",
    "mb": "Canada", "ns": "Canada", "nb": "Canada", "uk": "United Kingdom", "gb": "United Kingdom",
}
NAMED = (
    (r"\bunited kingdom\b|\bu\.k\.\b|\blondon\b|\bengland\b|\bscotland\b", "United Kingdom"),
    (r"\bcanada\b|\btoronto\b|\bvancouver\b|\bmontreal\b|\bottawa\b", "Canada"),
    (r"\bindia\b|\bbengaluru\b|\bbangalore\b|\bmumbai\b|\bhyderabad\b", "India"),
    (r"\bgermany\b|\bberlin\b|\bmunich\b|\bfrankfurt\b", "Germany"),
    (r"\bfrance\b|\bparis\b", "France"),
    (r"\bargentina\b|\bbuenos aires\b", "Argentina"),
    (r"\bmexico\b|\bmexico city\b", "Mexico"),
    (r"\bperu\b|\blima\b", "Peru"),
    (r"\bunited states\b|\busa\b|\bu\.s\.a?\b|\bu\.s\.\b", "United States"),
)


def iso_utc(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        seconds = number / 1000 if number > 10**11 else number
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def within_days(updated_at, posted_at, days: int = 45, now: datetime | None = None) -> bool:
    raw = posted_at or updated_at
    if raw in (None, ""):
        return True
    stamp = datetime.fromisoformat(iso_utc(raw).replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    return current - stamp <= timedelta(days=days)


def countries_in(location: str) -> list[str]:
    text = str(location or "").strip()
    if not text:
        return []
    if re.fullmatch(r"remote|anywhere|worldwide|distributed", text, re.I):
        return ["Remote"]
    found: list[str] = []
    for pattern, name in NAMED:
        if re.search(pattern, text, re.I) and name not in found:
            found.append(name)
    for code in re.findall(r",\s*([A-Za-z]{2})\b", text):
        if not code.isupper():
            continue
        lower = code.lower()
        if lower in NON_US_REGIONS and NON_US_REGIONS[lower] not in found:
            found.append(NON_US_REGIONS[lower])
        elif lower in US_STATES and "United States" not in found:
            found.append("United States")
    if not found:
        tail = text.split(",")[-1].strip()
        found.append(tail[:40] or "Other")
    if any(name != "United States" for name in found) and len(found) > 1:
        return found
    return found


def years_required(title: str, description: str):
    match = re.search(r"(\d{1,2})\s*\+?\s*(?:years|yrs|year|yr)\b", f"{title} {description}", re.I)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 40 else None


def seniority_of(title: str) -> str:
    text = str(title or "").lower()
    if re.search(r"\b(intern|internship)\b", text):
        return "intern"
    if re.search(r"\b(junior|jr\.?|entry[- ]level|associate)\b", text):
        return "junior"
    if re.search(r"\b(staff|principal)\b", text):
        return "staff"
    if re.search(r"\b(senior|sr\.?)\b", text):
        return "senior"
    if re.search(r"\b(director|vice president|\bvp\b|head of)\b", text):
        return "director"
    if re.search(r"\b(manager|lead)\b", text):
        return "manager"
    if re.search(r"\b(engineer|scientist|analyst|designer|developer|nurse|accountant|recruiter|specialist|technician)\b", text):
        return "mid"
    return ""


def sponsorship_of(text: str) -> str:
    blob = str(text or "").lower()
    if re.search(r"unable to sponsor|cannot sponsor|no sponsorship|without sponsorship|must be authorized to work|not (?:able|eligible) to sponsor", blob):
        return "no"
    if re.search(r"visa sponsorship|will sponsor|sponsorship available|h-1b|h1b", blob):
        return "yes"
    return "unknown"


def _amount(raw: str) -> int:
    number = raw.replace(",", "")
    if number.lower().endswith("k"):
        return int(float(number[:-1]) * 1000)
    return int(float(number))


def salary_from(text: str) -> dict:
    blob = str(text or "")
    hourly = re.search(
        r"(?P<cur>[$£€]|USD|EUR|GBP|CAD)?\s?(?P<n>\d{2,3}(?:\.\d{1,2})?)\s?(?:/|per)\s?(?:hour|hr)\b",
        blob,
        re.I,
    )
    if hourly and "billion" not in blob[max(0, hourly.start() - 20):hourly.end() + 20].lower():
        value = int(float(hourly.group("n")) * 2080)
        return {"salary_min": value, "salary_max": value, "currency": _currency(hourly.group("cur"))}
    yearly = re.search(
        r"(?P<cur>[$£€]|USD|EUR|GBP|CAD)?\s?(?P<a>\d{2,3}(?:,\d{3})+|\d{2,3}\s?[kK])\s?(?:-|–|to)\s?(?:[$£€]|USD|EUR|GBP|CAD)?\s?(?P<b>\d{2,3}(?:,\d{3})+|\d{2,3}\s?[kK])",
        blob,
    )
    if yearly:
        return {
            "salary_min": _amount(yearly.group("a").replace(" ", "")),
            "salary_max": _amount(yearly.group("b").replace(" ", "")),
            "currency": _currency(yearly.group("cur")),
        }
    return {"salary_min": None, "salary_max": None, "currency": ""}


def _currency(token: str | None) -> str:
    token = (token or "$").strip().upper()
    return {"$": "USD", "£": "GBP", "€": "EUR", "USD": "USD", "EUR": "EUR", "GBP": "GBP", "CAD": "CAD"}.get(token, "USD")
