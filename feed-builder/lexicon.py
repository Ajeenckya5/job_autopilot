"""Learn the vocabulary of every field from the postings themselves.

Nothing here is a hand-written list of skills, roles or stopwords. From the feed it works out:

- filler: words common in postings of every kind ("the", "our", "company", "mission"),
- skills: phrases several employers use that are concentrated in some kinds of job ("westlaw" in
  paralegal postings, "cdl" in driver postings, "pytorch" in machine learning postings),
- how rare each skill phrase is (inverse document frequency), so a rare match counts for more,
- how rare each title word is, and which job titles read alike ("paralegal", "legal assistant").

A "kind of job" is a word in the title, described by the employers that use it: one employer
posting the same job forty times counts once. Place names are learned from the location fields and
kept out. The site reads lexicon.json to find skills in a resume and in each posting.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

TOKEN = re.compile(r"[a-z0-9+#]+(?:[.&][a-z0-9+#]+)*")
# Punctuation that ends a phrase. Slashes and hyphens join words instead ("ci/cd", "cite-checking").
BREAK = re.compile(r"[,;:!?()\[\]{}|•·–—\"“”‘’*…]|\.(?=\s|$)|\s-\s|\n")
TITLE_BREAK = re.compile(r"\s[-–—|]\s|[,(\[:]")
MAX_N = 3
TEXT_LIMIT = 6000

# Thresholds on counts, never on words. Read off the September 2026 feed.
FILLER_MIN_SHARE = 0.2    # filler is in at least a fifth of postings
FILLER_SHARE = 0.8        # a word in 80% of postings is filler
FILLER_FLOOR = 0.5        # and so is one no kind of job uses less than half as often as average
FILLER_GROUP = 20         # judged on kinds of job with 20+ employers
PRIOR = 10                # employers' worth of shrinkage toward the average
MIN_COMPANIES = 3         # a skill phrase is used by 3+ employers
MAX_SHARE = 0.3           # and by fewer than 30% of postings
SKILL_GROUP = 4           # judged on kinds of job with 4+ employers
SKILL_PAIR = 2            # at least 2 of them use the phrase
SKILL_LIFT = 2.5          # at 2.5 times the average rate
SKILL_P = 1e-4            # and not by chance (binomial tail)
SKILL_SPREAD = 0.42       # used at its average rate or more by at most 42% of common kinds of job
MAX_PHRASES = 30000
TITLE_MIN_DOCS = 4
HEAD_SHARE = 0.1          # a word ending at least a tenth of the titles it is in names a job
RELATED_MIN = 0.3
RELATED_TOP = 8
CENTROID_TOP = 60


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    # A single letter joined by a hyphen is one word: "e-discovery", "k-12", "x-ray".
    text = re.sub(r"\b([a-z0-9])-(?=[a-z0-9])", r"\1", text)
    return text.replace("/", " ").replace("-", " ").replace("_", " ").replace("'", " ").replace("’", " ")


def segments(text: str) -> list[list[str]]:
    """Runs of words with no sentence punctuation between them."""
    return [TOKEN.findall(part) for part in BREAK.split(fold(text)) if part.strip()]


def is_word(token: str) -> bool:
    return len(token) >= 2 and not token.replace(".", "").isdigit()


def grams(parts: list[list[str]], filler: set[str]) -> set[str]:
    """Phrases of one to three words that start, end and (for three) pivot on a real word."""
    out: set[str] = set()
    for words in parts:
        ok = [is_word(tok) and tok not in filler for tok in words]
        for i in range(len(words)):
            if not ok[i]:
                continue
            out.add(words[i])
            if i + 1 < len(words) and ok[i + 1]:
                out.add(f"{words[i]} {words[i + 1]}")
                if i + 2 < len(words) and ok[i + 2]:
                    out.add(f"{words[i]} {words[i + 1]} {words[i + 2]}")
    return out


def place_tokens(jobs: list[dict]) -> set[str]:
    """Words that mostly appear in the location fields: city, state and country names."""
    in_places: Counter = Counter()
    in_titles: Counter = Counter()
    for job in jobs:
        blob = [job.get("location_raw") or ""]
        for row in job.get("locations") or []:
            blob.extend(str(row.get(key) or "") for key in ("city", "region", "country"))
        in_places.update({tok for part in segments(" ".join(blob)) for tok in part})
        in_titles.update({tok for part in segments(job.get("title") or "") for tok in part})
    return {tok for tok, count in in_places.items() if count >= 3 and count >= 2 * in_titles.get(tok, 0)}


def title_words(title: str, places: set[str]) -> list[str]:
    return [tok for part in segments(title) for tok in part if is_word(tok) and tok not in places]


def title_core(title: str, places: set[str]) -> list[str]:
    """The title before any comma, dash or bracket: "Senior Accountant" of "Senior Accountant - Remote"."""
    first = TITLE_BREAK.split(str(title or ""))[0]
    return title_words(first, places)


def title_heads(jobs: list[dict], places: set[str]) -> dict[str, float]:
    """How often each title word ends a title ("accountant" nearly always, "health" rarely): the
    site uses it to tell a job title on a resume from an employer or a department."""
    ends: Counter = Counter()
    seen: Counter = Counter()
    for job in jobs:
        core = title_core(job.get("title") or "", places)
        if not core:
            continue
        ends[core[-1]] += 1
        seen.update(set(core))
    return {tok: round(ends[tok] / count, 2) for tok, count in seen.items() if count >= 2 and ends[tok] / count >= HEAD_SHARE}


def binomial_tail(hits: int, trials: int, rate: float) -> float:
    """Chance of at least `hits` successes in `trials` at `rate`."""
    if hits <= 0:
        return 1.0
    if rate <= 0:
        return 0.0
    if rate >= 1:
        return 1.0
    log_term = (math.lgamma(trials + 1) - math.lgamma(hits + 1) - math.lgamma(trials - hits + 1)
                + hits * math.log(rate) + (trials - hits) * math.log1p(-rate))
    term = math.exp(log_term)
    total = 0.0
    k = hits
    ratio = rate / (1 - rate)
    while k <= trials and term > 0:
        total += term
        if term < total * 1e-12:
            break
        term *= (trials - k) / (k + 1) * ratio
        k += 1
    return min(1.0, total)


class Groups:
    """Postings grouped by (title word, employer)."""

    def __init__(self, jobs: list[dict], heads: list[set[str]]):
        self.units: dict[tuple[str, str], list[int]] = defaultdict(list)
        for index, (job, head) in enumerate(zip(jobs, heads)):
            company = (job.get("company") or "").strip().lower()
            for tok in head:
                self.units[(tok, company)].append(index)
        self.employers = Counter(tok for tok, _ in self.units)
        self.total_units = len(self.units)

    def profile(self, items: list[set[str]], wanted: set[str], min_group: int):
        """For each item: per kind of job, the employer-weighted share of postings that use it, how
        many employers use it, and in how many (kind, employer) units it appears overall."""
        weight: dict[str, dict[str, float]] = defaultdict(dict)
        spread: dict[str, dict[str, int]] = defaultdict(dict)
        unit_hits: Counter = Counter()
        for (group, _company), docs in self.units.items():
            counts: Counter = Counter()
            for index in docs:
                counts.update(items[index] & wanted)
            unit_hits.update(counts.keys())
            size = self.employers[group]
            if size < min_group:
                continue
            scale = 1 / (len(docs) * size)
            for item, count in counts.items():
                w = weight[item]
                w[group] = w.get(group, 0.0) + count * scale
                s = spread[item]
                s[group] = s.get(group, 0) + 1
        return weight, spread, unit_hits


def shrunk(share: float, employers: int, base: float) -> float:
    return (share * employers + base * PRIOR) / (employers + PRIOR)


def build(jobs: list[dict], texts: list[str] | None = None) -> dict:
    source = texts if texts is not None else [job.get("description_text") for job in jobs]
    keep = [i for i, job in enumerate(jobs) if job.get("title")]
    jobs = [jobs[i] for i in keep]
    texts = [str(source[i] or "")[:TEXT_LIMIT] for i in keep]
    total = len(jobs)
    if total < 200:
        return {"version": 1, "docs": total, "phrases": {}, "filler": [], "title_idf": {}, "heads": {}, "titles": {}}
    places = place_tokens(jobs)
    parts = [segments(text) for text in texts]
    heads = [set(title_words(job.get("title") or "", places)) for job in jobs]
    groups = Groups(jobs, heads)

    # 1. Filler: words common in every kind of job, never much rarer in any of them
    #    ("experience", "team", "skills"). A skill phrase may not start or end with one.
    words = [{tok for seg in segs for tok in seg} for segs in parts]
    word_df = Counter(tok for found in words for tok in found)
    common = {tok for tok, count in word_df.items() if count / total >= FILLER_MIN_SHARE}
    weight, _spread, _hits = groups.profile(words, common, FILLER_GROUP)
    big = [g for g, count in groups.employers.items() if count >= FILLER_GROUP]
    filler = set()
    for tok in common:
        base = word_df[tok] / total
        low = min((shrunk(weight[tok].get(g, 0.0), groups.employers[g], base) / base for g in big), default=1)
        if base >= FILLER_SHARE or low >= FILLER_FLOOR:
            filler.add(tok)
    del words, weight

    # 2. Candidate phrases used by enough employers, outside place names.
    doc_grams: list[set[str]] = []
    phrase_df: Counter = Counter()
    phrase_companies: dict[str, set] = defaultdict(set)
    for job, segs in zip(jobs, parts):
        found = {g for g in grams(segs, filler) if not any(tok in places for tok in g.split(" "))}
        doc_grams.append(found)
        phrase_df.update(found)
        company = (job.get("company") or "").strip().lower()
        for g in found:
            seen = phrase_companies[g]
            if len(seen) < MIN_COMPANIES:
                seen.add(company)
    candidates = {g for g, count in phrase_df.items()
                  if len(phrase_companies[g]) >= MIN_COMPANIES and count / total < MAX_SHARE}
    del phrase_companies, parts

    # 3. Skills: concentrated in some kind of job, by several employers, beyond chance, and not
    #    used at an average-or-higher rate by most common kinds of job ("track record" is).
    weight, spread, unit_hits = groups.profile(doc_grams, candidates, SKILL_GROUP)
    big = [g for g, count in groups.employers.items() if count >= FILLER_GROUP]
    phrases: dict[str, float] = {}
    for g in candidates:
        base = phrase_df[g] / total
        rate = unit_hits[g] / max(1, groups.total_units)
        rates = weight[g]
        above = sum(1 for k in big if shrunk(rates.get(k, 0.0), groups.employers[k], base) >= base)
        if big and above / len(big) > SKILL_SPREAD:
            continue
        for group, value in sorted(rates.items(), key=lambda row: -row[1]):
            hits = spread[g][group]
            if hits < SKILL_PAIR:
                continue
            employers = groups.employers[group]
            if shrunk(value, employers, base) / base < SKILL_LIFT:
                continue
            if binomial_tail(hits, employers, rate) <= SKILL_P:
                phrases[g] = math.log(total / phrase_df[g])
                break
    del weight, spread
    if len(phrases) > MAX_PHRASES:
        phrases = dict(sorted(phrases.items(), key=lambda row: row[1])[:MAX_PHRASES])

    # 4. Title words: how rare each is among titles, and which titles read alike.
    title_df = Counter(tok for head in heads for tok in head)
    title_idf = {tok: math.log(total / count) for tok, count in title_df.items() if count >= 2}
    related = related_titles(jobs, doc_grams, phrases, places)
    return {
        "version": 1,
        "docs": total,
        "phrases": {g: round(v, 1) for g, v in sorted(phrases.items())},
        "filler": sorted(filler),
        "title_idf": {tok: round(v, 1) for tok, v in sorted(title_idf.items())},
        "heads": dict(sorted(title_heads(jobs, places).items())),
        "titles": related,
    }


def title_terms(title: str, places: set[str]) -> set[str]:
    """What a title names: the last word of its core and the two- and three-word endings.
    "Tow Truck Driver - Dallas" names "driver", "truck driver" and "tow truck driver"."""
    core = title_core(title, places)
    return {" ".join(core[-n:]) for n in range(1, MAX_N + 1) if len(core) >= n}


def related_titles(jobs, doc_grams, phrases, places) -> dict[str, list]:
    """Titles whose postings ask for the same distinctive things, closest first, 0..1 closeness.
    A title's profile is the skill phrases its employers use far more often than postings overall."""
    total = len(jobs)
    df = Counter(g for found in doc_grams for g in found if g in phrases)
    term_docs: dict[str, list[int]] = defaultdict(list)
    whole: Counter = Counter()
    for index, job in enumerate(jobs):
        title = job.get("title") or ""
        for term in title_terms(title, places):
            term_docs[term].append(index)
        core = " ".join(title_core(title, places))
        if core:
            whole[core] += 1
    vectors: dict[str, dict[str, float]] = {}
    for term, docs in term_docs.items():
        companies: Counter = Counter((jobs[i].get("company") or "").lower() for i in docs)
        if len(docs) < TITLE_MIN_DOCS or len(companies) < 2:
            continue
        acc: Counter = Counter()
        for index in docs:
            share = 1 / (companies[(jobs[index].get("company") or "").lower()] * len(companies))
            for g in doc_grams[index]:
                if g in phrases:
                    acc[g] += share
        profile = {}
        for g, rate in acc.items():
            base = df[g] / total
            if rate > 2 * base:
                profile[g] = rate * math.log(rate / base)
        top = sorted(profile.items(), key=lambda row: -row[1])[:CENTROID_TOP]
        norm = math.sqrt(sum(v * v for _, v in top)) or 1
        if top:
            vectors[term] = {g: v / norm for g, v in top}
    postings: dict[str, list] = defaultdict(list)
    for term, vec in vectors.items():
        if whole[term] < 2:
            continue  # only titles people actually use as a whole are offered as related
        for g, v in vec.items():
            postings[g].append((term, v))
    out: dict[str, list] = {}
    for term, vec in vectors.items():
        scores: Counter = Counter()
        for g, v in vec.items():
            for other, w in postings[g]:
                if other != term:
                    scores[other] += v * w
        own = set(term.split(" "))
        rows = []
        for other, sim in scores.most_common():
            if sim < RELATED_MIN or len(rows) >= RELATED_TOP:
                break
            words = set(other.split(" "))
            # "senior paralegal" already contains "paralegal": only new titles are worth listing.
            if own <= words or words <= own:
                continue
            rows.append([other, round(sim, 2)])
        if rows:
            out[term] = rows
    return dict(sorted(out.items()))


def write(jobs: list[dict], out: Path, texts: list[str] | None = None) -> dict:
    lexicon = build(jobs, texts)
    out.write_text(json.dumps(lexicon, separators=(",", ":"), ensure_ascii=False))
    return lexicon
