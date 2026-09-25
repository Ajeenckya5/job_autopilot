import { getLexicon, isFiller, isTitlePhrase, phraseIdf, phrasesIn, phraseSet, relatedTitles, titleCore, titleIdf, titleWords } from "./lexicon.js";
import { displayForm, titlesFromResume } from "./resume.js";
import { clampLookback, cosine, embed, fnv } from "./text.js";

/**
 * Matching for any field. Nothing here knows what a nurse or an engineer is:
 * - role fit compares a posting's title with the titles the person typed and has held, word by word,
 *   rarer title words counting for more, plus titles whose postings read alike (from the lexicon);
 * - skill fit is how many of the posting's most telling phrases the resume also uses, rarer phrases
 *   counting for more.
 */

const JOB_EMBED = new WeakMap();
const JOB_UNITS = new WeakMap();
const SKILLS = new WeakMap();
const KEY_PHRASES = 12;
const PROFILE_MIN = 6;

export function yearsFromResume(text, now = Date.now()) {
  const year = new Date(now).getUTCFullYear();
  const ranges = [];
  const re = /\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2}|present|current|now)\b/gi;
  let match = re.exec(String(text || ""));
  while (match) {
    const start = Number(match[1]);
    const end = /present|current|now/i.test(match[2]) ? year : Number(match[2]);
    if (end >= start && end - start <= 50) ranges.push([start, end]);
    match = re.exec(String(text || ""));
  }
  ranges.sort((a, b) => a[0] - b[0]);
  let cursor = -1;
  let total = 0;
  ranges.forEach(([start, end]) => {
    const from = Math.max(start, cursor);
    if (end > from) total += end - from;
    cursor = Math.max(cursor, end);
  });
  return total;
}

export function seniorityOf(text) {
  const blob = String(text || "").toLowerCase();
  if (/\b(intern(ship)?|co-?op|student|new grad)\b/.test(blob)) return "intern";
  if (/\b(junior|jr)\b/.test(blob)) return "junior";
  if (/\b(staff|principal)\b/.test(blob)) return "staff";
  if (/\b(senior|sr)\b/.test(blob)) return "senior";
  if (/\b(director|vice president|\bvp\b|head of)\b/.test(blob)) return "director";
  if (/\b(manager|lead)\b/.test(blob)) return "manager";
  return "mid";
}

function clampWeight(weight) {
  return Math.max(0, Math.min(1, weight));
}

function normalize(vector) {
  let norm = 0;
  for (let i = 0; i < vector.length; i += 1) norm += vector[i] * vector[i];
  norm = Math.sqrt(norm) || 1;
  return vector.map((value) => value / norm);
}

/** "ml engineer" → "ML Engineer", "fp&a analyst" → "FP&A Analyst": two-letter title words are initials. */
function titleCase(text) {
  return String(text || "").split(" ").map((word) => {
    if ((word.length === 2 && !isFiller(word)) || (word.includes("&") && word.length <= 5)) return word.toUpperCase();
    return word.charAt(0).toUpperCase() + word.slice(1);
  }).join(" ");
}

/** A title as weighted words: "Senior Staff Accountant" → senior, staff, accountant, rarer words weighing more. */
function roleSpec(label, kind, weight, id) {
  const words = [...new Set(titleCore(label))].filter((word) => !isFiller(word));
  // The last word says what the job is ("scientist" in "data scientist"), so it counts twice.
  const weights = words.map((word, index) => titleIdf(word) * (index === words.length - 1 ? 2 : 1));
  const total = weights.reduce((sum, value) => sum + value, 0);
  return { id: id || words.join(" "), label: String(label || "").trim(), kind, weight, words, weights, total };
}

function relatedWeight(similarity) {
  return clampWeight(0.5 + 0.6 * (similarity - 0.3));
}

/**
 * The titles a search looks for: what the person typed (weight 1), titles on their resume (0.9),
 * and titles whose postings read like those (0.5 to 0.8). Hidden ones and nudges from feedback apply.
 */
export function familyMap(profile) {
  const source = profile || {};
  const hidden = new Set([...(source.hidden_roles || []), ...(source.hidden_phrases || [])].map((value) => String(value).toLowerCase()));
  const nudges = (source.feedback && source.feedback.roleNudges) || {};
  const map = new Map();
  const consider = (spec) => {
    if (!spec.words.length || hidden.has(spec.id) || hidden.has(spec.label.toLowerCase())) return;
    const nudged = { ...spec, weight: clampWeight(spec.weight + (nudges[spec.id] || 0)) };
    if (spec.kind === "related") nudged.weight = Math.min(nudged.weight, 0.85);
    const prev = map.get(spec.id);
    if (!prev || nudged.weight > prev.weight) map.set(spec.id, nudged);
  };
  const typed = (source.roles || []).map((role) => roleSpec(role, "target", 1));
  const held = (Array.isArray(source.titles) && source.titles.length ? source.titles : titlesFromResume(source.resume_text || ""))
    .map((title) => roleSpec(title, "resume", 0.9));
  typed.forEach(consider);
  held.forEach(consider);
  [...typed, ...held].forEach((spec) => {
    const scale = spec.kind === "target" ? 1 : 0.9;
    relatedTitles(spec.label).forEach(({ term, weight }) => {
      consider(roleSpec(term, "related", relatedWeight(weight) * scale, term));
    });
  });
  return map;
}

/** Related titles the search adds, as chips the person can remove. */
export function familyChips(profile) {
  const chips = [];
  familyMap(profile || {}).forEach((entry) => {
    if (entry.kind !== "related") return;
    chips.push({ key: `role:${entry.id}`, id: entry.id, label: titleCase(entry.label), kind: "related", weight: entry.weight });
  });
  return chips.sort((a, b) => b.weight - a.weight || a.label.localeCompare(b.label)).slice(0, 10);
}

/** Titles the person has held, read from the resume. */
export function rolesMentioned(text) {
  return titlesFromResume(text);
}

function applyFeedback(mixed, profile) {
  const feedback = profile.feedback;
  let next = mixed;
  if (feedback?.posCount && feedback.posMean) {
    next = next.map((value, index) => value + 0.1 * (feedback.posMean[index] || 0));
  }
  if (feedback?.negCount && feedback.negMean) {
    next = next.map((value, index) => value - 0.1 * (feedback.negMean[index] || 0));
  }
  return normalize(next);
}

function profileVector(profile, family) {
  if (profile.rank_without_embeddings) return null;
  if (Array.isArray(profile.vector) && profile.vector.length === 384) {
    return applyFeedback(profile.vector.map((value) => Number(value) || 0), profile);
  }
  const titles = [];
  family.forEach((entry) => {
    if (entry.kind !== "related") titles.push(entry.label);
  });
  const resume = embed(profile.resume_text || titles.join(" "));
  const target = embed(titles.join(" ") || profile.resume_text || "");
  const mixed = normalize(resume.map((value, index) => 0.5 * value + 0.5 * target[index]));
  return applyFeedback(mixed, profile);
}

/** Every phrase the resume and skill list use, including shorter ones inside longer ones. */
export function resumeTerms(profile) {
  const source = profile || {};
  const terms = new Set(Array.isArray(source.resume_terms) ? source.resume_terms : []);
  phraseSet(source.resume_text || "").forEach((phrase) => terms.add(phrase));
  (source.skills || []).forEach((skill) => {
    const words = titleWords(skill);
    if (words.length) terms.add(words.join(" "));
    phraseSet(skill).forEach((phrase) => terms.add(phrase));
  });
  return terms;
}

export function prepareProfile(profile, now = Date.now()) {
  const source = profile || {};
  const family = familyMap(source);
  return {
    family,
    specs: [...family.values()],
    terms: resumeTerms(source),
    years: source.years == null || source.years === ""
      ? yearsFromResume(source.resume_text || "", now)
      : Number(source.years) || 0,
    vector: profileVector(source, family),
    days: clampLookback(source.lookback_days),
    now,
  };
}

const PREFERRED = /preferred|nice to have|nice-to-have|plus|bonus|desired/;
const REQUIRED = /required|must have|must |minimum qualification|minimum requirements|basic qualification/;

function unitOf(phrase, required, first) {
  const key = titleWords(phrase).join(" ");
  return { phrase: key, idf: phraseIdf(key) || 4, required, count: 1, first };
}

/**
 * The phrases a posting uses that postings treat as skills, each marked required or preferred by
 * the section it first appears in.
 */
export function splitSkills(text) {
  const units = new Map();
  let mode = "required";
  let offset = 0;
  String(text || "").split(/\n+|(?<=\.)\s+/).forEach((part) => {
    const low = part.toLowerCase();
    if (PREFERRED.test(low) && !REQUIRED.test(low)) mode = "preferred";
    else if (REQUIRED.test(low)) mode = "required";
    phrasesIn(part).forEach((row) => {
      const prev = units.get(row.phrase);
      if (prev) {
        prev.count += row.count;
        prev.required = prev.required || mode === "required";
      } else {
        units.set(row.phrase, { phrase: row.phrase, idf: row.idf, required: mode === "required", count: row.count, first: offset + row.first });
      }
    });
    offset += part.length;
  });
  const rows = [...units.values()];
  return {
    required: rows.filter((row) => row.required).map((row) => row.phrase),
    preferred: rows.filter((row) => !row.required).map((row) => row.phrase),
    units: rows,
  };
}

/** Every skill phrase a posting uses, minus words from the employer's own name ("Reddit" at Reddit). */
function jobUnits(job) {
  const cached = JOB_UNITS.get(job);
  if (cached && cached.lexicon === getLexicon()) return cached.units;
  let units;
  if (Array.isArray(job.skills_required) || Array.isArray(job.skills_preferred)) {
    units = (job.skills_required || []).map((skill, i) => unitOf(skill, true, i))
      .concat((job.skills_preferred || []).map((skill, i) => unitOf(skill, false, 100 + i)));
  } else {
    units = splitSkills(`${job.title || ""}\n${job.description_text || ""}`).units;
  }
  const own = new Set(titleWords(job.company).filter((word) => !isFiller(word)));
  const out = units
    .filter((unit) => unit.phrase && !unit.phrase.split(" ").some((word) => own.has(word)))
    .map((unit) => ({ ...unit, base: unit.idf * (1 + Math.log(unit.count)) * (unit.required ? 1 : 0.5), title: isTitlePhrase(unit.phrase) }));
  JOB_UNITS.set(job, { lexicon: getLexicon(), units: out });
  return out;
}

/**
 * What postings for the person's own titles have in common: for each phrase, the share of those
 * postings that use it. A phrase most of them share ("pytorch" for machine learning engineers) is a
 * skill of the role; one a single employer uses ("orbit" at a space company) is that employer's world.
 */
function roleProfile(rows, terms) {
  const chosen = rows.filter((row) => row.role.weight >= 0.85 && (row.role.kind === "target" || row.role.kind === "resume"));
  if (chosen.length < PROFILE_MIN) {
    // Few postings carry the person's titles: lean on the postings that share most of the resume.
    const picked = new Set(chosen);
    rows
      .filter((row) => !picked.has(row))
      .map((row) => ({ row, overlap: resumeOverlap(row.job, terms) }))
      .filter((item) => item.overlap > 0)
      .sort((a, b) => b.overlap - a.overlap)
      .slice(0, PROFILE_MIN - chosen.length)
      .forEach((item) => chosen.push(item.row));
  }
  const counts = new Map();
  chosen.forEach((row) => {
    jobUnits(row.job).forEach((unit) => counts.set(unit.phrase, (counts.get(unit.phrase) || 0) + 1));
  });
  return { counts, size: chosen.length };
}

/** How much of a posting's vocabulary the resume shares, rarer phrases counting for more. */
function resumeOverlap(job, terms) {
  let shared = 0;
  let total = 0;
  jobUnits(job).forEach((unit) => {
    total += unit.idf;
    if (terms.has(unit.phrase)) shared += unit.idf;
  });
  return total ? shared / Math.sqrt(total) : 0;
}

function typicality(phrase, profile) {
  if (!profile || profile.size < 3) return 1;
  return ((profile.counts.get(phrase) || 0) + 0.5) / (profile.size + 1);
}

/** 1 when the resume uses the phrase; partly when it uses a part of it ("python" of "python sdk"). */
function credit(unit, terms) {
  if (terms.has(unit.phrase)) return 1;
  const words = unit.phrase.split(" ");
  if (words.length < 2) return 0;
  let best = 0;
  for (let n = words.length - 1; n >= 1; n -= 1) {
    for (let i = 0; i + n <= words.length; i += 1) {
      const part = words.slice(i, i + n).join(" ");
      if (terms.has(part)) best = Math.max(best, Math.min(1, (phraseIdf(part) || 0) / unit.idf));
    }
  }
  return best;
}

// A resume rarely repeats a posting word for word: sharing this share of the posting's most
// telling phrases counts as a full skill match.
const FULL_SHARE = 0.5;

const LABELS = new WeakMap();

/** The posting's own spelling of a phrase, remembered so a re-rank does not search the text again. */
function labelFor(job, phrase) {
  let cache = LABELS.get(job);
  if (!cache) {
    const text = `${job.title || ""}\n${job.description_text || ""}`;
    cache = { text, lower: text.toLowerCase(), labels: new Map() };
    LABELS.set(job, cache);
  }
  if (!cache.labels.has(phrase)) cache.labels.set(phrase, displayForm(phrase, cache.text, cache.lower));
  return cache.labels.get(phrase);
}

function skillFit(job, prepared, profile) {
  const all = jobUnits(job);
  const weights = new Float64Array(all.length);
  const order = new Array(all.length);
  for (let i = 0; i < all.length; i += 1) {
    weights[i] = all[i].base * typicality(all[i].phrase, profile);
    order[i] = i;
  }
  if (all.length > KEY_PHRASES) order.sort((a, b) => weights[b] - weights[a] || all[a].first - all[b].first);
  const units = [];
  for (let k = 0; k < Math.min(KEY_PHRASES, order.length); k += 1) units.push({ unit: all[order[k]], weight: weights[order[k]] });
  if (!units.length) return { fit: 0.5, share: 0, matched: [], missing: [], requiredMatched: 0 };
  let have = 0;
  let want = 0;
  const matched = [];
  const missing = [];
  units.forEach(({ unit, weight }) => {
    const got = credit(unit, prepared.terms);
    // A job title in the text counts when the resume shares it; role fit already judges titles, so
    // one the resume lacks is not also a missing skill. Titles are never listed as skills.
    if (unit.title) {
      if (got > 0) {
        have += weight * got;
        want += weight;
      }
      return;
    }
    have += weight * got;
    want += weight;
    if (got >= 0.99) matched.push({ unit, weight });
    // Missing means most postings for the role ask for it, not one employer's own world.
    else if (got === 0 && unit.required && (!profile || profile.size < 3 || typicality(unit.phrase, profile) >= 0.2)) missing.push({ unit, weight });
  });
  const share = want ? have / want : 0;
  const label = (unit) => labelFor(job, unit.phrase);
  const top3 = (rows) => rows.sort((a, b) => b.weight - a.weight).slice(0, 3).map((row) => label(row.unit));
  return {
    fit: Math.min(1, share / FULL_SHARE),
    share,
    matched: top3(matched),
    missing: top3(missing),
    requiredMatched: matched.filter((row) => row.unit.required).length,
  };
}

/** How much of a title's weight the posting's title carries: 1 when every word is there. */
function coverage(spec, words) {
  if (!spec.total) return 0;
  let got = 0;
  spec.words.forEach((word, index) => {
    if (words.has(word)) got += spec.weights[index];
  });
  return got / spec.total;
}

function roleFit(job, prepared) {
  const words = new Set(titleWords(job.title));
  let best = { weight: 0, kind: "", title: "", id: "", phrase: "" };
  prepared.specs.forEach((spec) => {
    const share = coverage(spec, words);
    let weight = 0;
    let kind = spec.kind;
    if (share >= 0.999) weight = spec.weight;
    else if (spec.kind !== "related" && share > 0.3) {
      // Part of a typed or held title: "Senior Accountant" for "Staff Accountant".
      weight = spec.weight * 0.7 * ((share - 0.3) / 0.7);
      kind = "partial";
    }
    if (weight > best.weight) best = { weight, kind, title: spec.label, id: spec.id, phrase: spec.words.join(" ") };
  });
  return best;
}

function experienceFit(have, required) {
  if (required == null || required === "") return 0.8;
  const gap = have - Number(required);
  if (gap >= -1 && gap <= 3) return 1;
  const beyond = gap < -1 ? -1 - gap : gap - 3;
  return Math.max(0, 1 - beyond * 0.2);
}

/**
 * An intern posting for someone four years in, or a staff title for someone two years in, is a
 * stretch the title already tells us about. 1 means the level fits. "Manager" and "Lead" are left
 * out: in "Product Manager" or "Retail Partner Lead" they name the job, not a level.
 */
export function seniorityFit(title, years) {
  const level = seniorityOf(title);
  const have = Number(years) || 0;
  if (!have) return 1; // no dates on the resume: the level is unknown, so it cannot count against a job
  if (level === "intern") return have >= 2 ? 0.25 : 1;
  if (level === "junior") return have > 5 ? 0.7 : 1;
  if (level === "staff") return have >= 7 ? 1 : have >= 5 ? 0.75 : 0.55;
  if (level === "director") return have >= 10 ? 1 : 0.35;
  return 1;
}

const LEVEL_WORDS = { intern: "Intern or student role", junior: "Junior role", staff: "Staff or principal level", director: "Director level" };

function relationLabel(match) {
  if (!match || match.weight <= 0) return "";
  if (match.kind === "target") return "Your target";
  if (match.kind === "resume") return "Your past title";
  if (match.kind === "partial") return `Similar title: ${titleCase(match.title)}`;
  return `Related: ${titleCase(match.title)}`;
}

export function tierOf(score) {
  if (score >= 85) return "strong";
  if (score >= 70) return "good";
  if (score >= 55) return "stretch";
  return "hide";
}

function vectorOf(job) {
  if (Array.isArray(job.embedding) && job.embedding.length === 384) return job.embedding;
  const cached = JOB_EMBED.get(job);
  if (cached) return cached;
  const vector = embed(`${job.title || ""} ${job.description_text || ""}`);
  JOB_EMBED.set(job, vector);
  return vector;
}

function freshness(posted, days, now) {
  if (!posted) return 0.45;
  const t = new Date(posted).getTime();
  if (Number.isNaN(t)) return 0.45;
  const age = Math.max(0, now - t) / 86400000;
  return Math.max(0, 1 - age / days);
}

function experienceLine(required, have) {
  if (required == null || required === "") return "";
  return `Asks ${required}+ years, you have ${Math.round(have)}`;
}

export function scoreAll(jobs, profile, now = Date.now()) {
  const prepared = prepareProfile(profile, now);
  const roleCache = new Map();
  const rows = (jobs || []).map((job) => {
    const key = String(job.title || "");
    let role = roleCache.get(key);
    if (!role) {
      role = roleFit(job, prepared);
      role.relation = relationLabel(role);
      roleCache.set(key, role);
    }
    return { job, role };
  });
  const shared = roleProfile(rows, prepared.terms);
  // Skill fit depends only on the resume's phrases and what the role's postings share, so a re-rank
  // after hiding a related title reuses it.
  const key = `${fnv([...prepared.terms].sort().join("|"))}:${shared.size}:${fnv([...shared.counts].map(([k, v]) => `${k}=${v}`).sort().join("|"))}:${getLexicon().docs}`;
  rows.forEach((row) => {
    const cached = SKILLS.get(row.job);
    if (cached && cached.key === key) {
      row.skills = cached.skills;
      return;
    }
    row.skills = skillFit(row.job, prepared, shared);
    SKILLS.set(row.job, { key, skills: row.skills });
  });
  const external = Array.isArray(profile && profile.vector) && profile.vector.length === 384;
  const similarity = rows.map((row) => {
    if (!prepared.vector) return 0.5;
    const jobVector = external
      ? (Array.isArray(row.job.embedding) && row.job.embedding.length === 384 ? row.job.embedding : null)
      : vectorOf(row.job);
    if (!jobVector) return 0.5;
    return Math.max(0, Math.min(1, cosine(prepared.vector, jobVector)));
  });
  const sorted = Float64Array.from(similarity).sort();
  const floor = sorted.length > 500 ? sorted[sorted.length - 500] : -Infinity;
  const ranked = rows.map((row, index) => {
    const candidate = row.role.weight > 0 || row.skills.requiredMatched >= 3 || similarity[index] >= floor;
    const years = row.job.years_required == null ? null : Number(row.job.years_required);
    const exp = experienceFit(prepared.years, years);
    const fresh = freshness(row.job.posted_at, prepared.days, now);
    // A posting that states the years it wants has said its level; the title's words need not guess.
    const level = years != null && Number.isFinite(years) ? Math.max(seniorityFit(row.job.title, prepared.years), 0.75) : seniorityFit(row.job.title, prepared.years);
    const raw = 100 * (0.35 * row.role.weight + 0.35 * row.skills.fit + 0.15 * exp + 0.1 * similarity[index] + 0.05 * fresh) * Math.sqrt(level);
    const score = Math.round(Math.max(0, Math.min(100, raw)));
    const tier = candidate ? tierOf(score) : "hide";
    return {
      ...row.job,
      match_score: score,
      tier,
      bucket: tier === "hide" ? "possible" : "match",
      relation: row.role.relation,
      role_id: row.role.id || "",
      why_matched: row.skills.matched,
      why_missing: row.skills.missing,
      experience_line: experienceLine(years, prepared.years)
        || (level < 1 ? `${LEVEL_WORDS[seniorityOf(row.job.title)]}, you have ${Math.round(prepared.years)} years` : ""),
      semantic: similarity[index],
    };
  });
  ranked.sort((a, b) => b.match_score - a.match_score || String(b.posted_at).localeCompare(String(a.posted_at)));
  return ranked;
}

function runningMean(mean, count, vector) {
  if (!count || !mean) return vector.slice();
  return mean.map((value, index) => (value * count + vector[index]) / (count + 1));
}

export function noteFeedback(profile, job, sign) {
  const vector = vectorOf(job);
  const previous = profile.feedback || { posMean: null, posCount: 0, negMean: null, negCount: 0, roleNudges: {} };
  const feedback = {
    posMean: previous.posMean,
    posCount: previous.posCount || 0,
    negMean: previous.negMean,
    negCount: previous.negCount || 0,
    roleNudges: { ...(previous.roleNudges || {}) },
  };
  if (sign > 0) {
    feedback.posMean = runningMean(feedback.posMean, feedback.posCount, vector);
    feedback.posCount += 1;
  } else {
    feedback.negMean = runningMean(feedback.negMean, feedback.negCount, vector);
    feedback.negCount += 1;
  }
  if (job.role_id) {
    const next = (feedback.roleNudges[job.role_id] || 0) + sign * 0.05;
    feedback.roleNudges[job.role_id] = Math.max(-0.4, Math.min(0.4, next));
  }
  return { ...profile, feedback };
}

export function resetFeedback(profile) {
  const next = { ...profile };
  delete next.feedback;
  return next;
}
