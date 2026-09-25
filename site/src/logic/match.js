import taxonomy from "../data/taxonomy.json";
import { allSkillsIn } from "./resume.js";
import { clampLookback, cosine, embed } from "./text.js";

const ROLES = taxonomy.roles;
const BY_ID = new Map(ROLES.map((role) => [role.id, role]));
const ROLE_EMBED = new Map();
const JOB_EMBED = new WeakMap();

const PHRASES = ROLES.flatMap((role) => {
  const rows = [{ phrase: role.title.toLowerCase(), id: role.id }];
  (role.synonyms || []).forEach((synonym) => rows.push({ phrase: synonym.toLowerCase(), id: role.id }));
  return rows;
}).sort((a, b) => b.phrase.length - a.phrase.length);

export const TAXONOMY_ATTRIBUTION = taxonomy.attribution;

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

function escapePhrase(phrase) {
  return phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
}

function hasPhrase(title, phrase) {
  if (!phrase) return false;
  return new RegExp(`(?:^|[^a-z0-9+])${escapePhrase(phrase)}(?=$|[^a-z0-9+])`, "i").test(String(title || ""));
}

export function roleIdForTitle(title) {
  const blob = String(title || "");
  const hit = PHRASES.find((row) => hasPhrase(blob, row.phrase));
  return hit ? hit.id : "";
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

function meanVectors(vectors) {
  if (!vectors.length) return null;
  const out = new Array(vectors[0].length).fill(0);
  vectors.forEach((vector) => {
    for (let i = 0; i < out.length; i += 1) out[i] += vector[i];
  });
  for (let i = 0; i < out.length; i += 1) out[i] /= vectors.length;
  return normalize(out);
}

function roleEmbedding(role) {
  if (ROLE_EMBED.has(role.id)) return ROLE_EMBED.get(role.id);
  const vector = embed(`${role.title} ${(role.synonyms || []).join(" ")}`);
  ROLE_EMBED.set(role.id, vector);
  return vector;
}

function consider(map, id, weight, kind, hidden, nudges) {
  if (!id || hidden.has(id)) return;
  const role = BY_ID.get(id);
  if (!role) return;
  let nudged = clampWeight(weight + (nudges[id] || 0));
  if (kind === "adjacent") nudged = Math.min(nudged, 0.75);
  const prev = map.get(id);
  if (!prev || nudged > prev.weight) {
    map.set(id, { id, weight: nudged, kind, role });
    return;
  }
  if (nudged === prev.weight && kind === "target") prev.kind = "target";
}

function idsInText(text) {
  const found = [];
  const seen = new Set();
  PHRASES.forEach((row) => {
    if (seen.has(row.id)) return;
    if (hasPhrase(text, row.phrase)) {
      seen.add(row.id);
      found.push(row.id);
    }
  });
  return found;
}

export function rolesMentioned(text) {
  return idsInText(text).map((id) => BY_ID.get(id)?.title).filter(Boolean);
}

export function familyMap(profile) {
  const hidden = new Set(profile.hidden_roles || []);
  const nudges = (profile.feedback && profile.feedback.roleNudges) || {};
  const map = new Map();
  const targets = [];
  (profile.roles || []).forEach((role) => {
    idsInText(role).forEach((id) => {
      if (!targets.includes(id)) targets.push(id);
    });
  });
  targets.forEach((id) => {
    consider(map, id, 1, "target", hidden, nudges);
    (BY_ID.get(id).adjacent || []).forEach((adj) => consider(map, adj.id, adj.weight, "adjacent", hidden, nudges));
  });
  const past = []
    .concat(Array.isArray(profile.titles) ? profile.titles : [])
    .concat(profile.resume_text || "")
    .join("\n");
  idsInText(past).forEach((id) => {
    if (map.get(id)?.kind === "target") return;
    consider(map, id, 0.9, "resume", hidden, nudges);
    (BY_ID.get(id).adjacent || []).forEach((adj) => consider(map, adj.id, adj.weight * 0.9, "adjacent", hidden, nudges));
  });
  return map;
}

function compiledPhrase(phrase, weight, kind, id, title) {
  return {
    phrase,
    weight,
    kind,
    id,
    title,
    re: new RegExp(`(?:^|[^a-z0-9+])${escapePhrase(phrase)}(?=$|[^a-z0-9+])`, "i"),
  };
}

function phraseRows(family, hiddenPhrases) {
  const rows = [];
  family.forEach((entry) => {
    rows.push(compiledPhrase(entry.role.title.toLowerCase(), entry.weight, entry.kind, entry.id, entry.role.title));
    const synonymWeight = Math.min(entry.weight, 0.95);
    const synonymKind = entry.kind === "target" || entry.kind === "resume" ? "synonym" : entry.kind;
    (entry.role.synonyms || []).forEach((synonym) => {
      const phrase = synonym.toLowerCase();
      if (hiddenPhrases.has(phrase)) return;
      rows.push(compiledPhrase(phrase, synonymWeight, synonymKind, entry.id, entry.role.title));
    });
  });
  rows.sort((a, b) => b.phrase.length - a.phrase.length || b.weight - a.weight);
  return rows;
}

export function familyChips(profile) {
  const family = familyMap(profile || {});
  const hiddenPhrases = new Set((profile.hidden_phrases || []).map((phrase) => String(phrase).toLowerCase()));
  const typed = new Set((profile.roles || []).map((role) => String(role).toLowerCase()));
  const chips = [];
  const seen = new Set();
  family.forEach((entry) => {
    if (entry.kind === "target") {
      (entry.role.synonyms || []).forEach((synonym) => {
        const label = synonym.toLowerCase();
        if (typed.has(label) || hiddenPhrases.has(label) || seen.has(label)) return;
        seen.add(label);
        chips.push({ key: `syn:${entry.id}:${label}`, id: entry.id, label: synonym, kind: "synonym" });
      });
      return;
    }
    const label = entry.role.title.toLowerCase();
    if (hiddenPhrases.has(label) || seen.has(label)) return;
    seen.add(label);
    chips.push({ key: `role:${entry.id}`, id: entry.id, label: entry.role.title, kind: entry.kind });
  });
  return chips.sort((a, b) => a.label.localeCompare(b.label));
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
  const resume = embed(profile.resume_text || (profile.roles || []).join(" "));
  const targets = [];
  family.forEach((entry) => {
    if (entry.kind === "target") targets.push(roleEmbedding(entry.role));
  });
  const mean = meanVectors(targets) || resume;
  const mixed = normalize(resume.map((value, index) => 0.5 * value + 0.5 * mean[index]));
  return applyFeedback(mixed, profile);
}

export function prepareProfile(profile, now = Date.now()) {
  const source = profile || {};
  const family = familyMap(source);
  const hiddenPhrases = new Set((source.hidden_phrases || []).map((phrase) => String(phrase).toLowerCase()));
  const skills = new Set([...(source.skills || []), ...allSkillsIn(source.resume_text || "")]);
  return {
    family,
    phrases: phraseRows(family, hiddenPhrases),
    skills,
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

export function splitSkills(text) {
  const required = new Set();
  const preferred = new Set();
  let mode = "required";
  String(text || "").split(/\n+|(?<=\.)\s+/).forEach((part) => {
    const low = part.toLowerCase();
    if (PREFERRED.test(low) && !REQUIRED.test(low)) mode = "preferred";
    else if (REQUIRED.test(low)) mode = "required";
    allSkillsIn(part).forEach((skill) => (mode === "preferred" ? preferred : required).add(skill));
  });
  required.forEach((skill) => preferred.delete(skill));
  return { required: [...required], preferred: [...preferred] };
}

function jobSkills(job) {
  if (Array.isArray(job.skills_required) || Array.isArray(job.skills_preferred)) {
    return {
      required: job.skills_required || [],
      preferred: job.skills_preferred || [],
    };
  }
  return splitSkills(`${job.title || ""} ${job.description_text || ""}`);
}

function skillFit(job, prepared) {
  const skills = jobSkills(job);
  const matched = [];
  const missing = [];
  skills.required.forEach((skill) => (prepared.skills.has(skill) ? matched.push(skill) : missing.push(skill)));
  const preferredHits = skills.preferred.filter((skill) => prepared.skills.has(skill)).length;
  const denom = skills.required.length + 0.5 * skills.preferred.length;
  const numer = matched.length + 0.5 * preferredHits;
  return {
    fit: denom ? numer / denom : 0.5,
    matched: matched.slice(0, 3),
    missing: missing.slice(0, 3),
    requiredMatched: matched.length,
  };
}

function titleHasWords(title, phrase) {
  return phrase.split(/\s+/).filter(Boolean).every((word) => hasPhrase(title, word));
}

function roleFit(job, prepared) {
  const title = String(job.title || "");
  let best = null;
  for (let i = 0; i < prepared.phrases.length; i += 1) {
    const row = prepared.phrases[i];
    if (best && row.phrase.length < best.phrase.length) break;
    if (!row.re.test(title)) continue;
    if (!best || row.phrase.length > best.phrase.length || row.weight > best.weight) best = row;
  }
  if (!best || best.kind === "adjacent") {
    prepared.phrases.forEach((row) => {
      if (row.kind !== "target" && row.kind !== "synonym") return;
      if (row.phrase.split(/\s+/).length < 2) return;
      if (!titleHasWords(title, row.phrase)) return;
      if (!best || row.weight > best.weight) best = row;
    });
  }
  if (best) return best;
  if (job.role_id && prepared.family.has(job.role_id)) {
    const entry = prepared.family.get(job.role_id);
    return { weight: entry.weight, kind: entry.kind, title: entry.role.title, id: entry.id, phrase: entry.role.title };
  }
  return { weight: 0, kind: "", title: "", id: job.role_id || "", phrase: "" };
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
  if (match.kind === "target" || match.kind === "synonym") return "Your target";
  return `Related: ${match.title}`;
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
    const key = `${job.role_id || ""}\n${job.title || ""}`;
    let role = roleCache.get(key);
    if (!role) {
      role = roleFit(job, prepared);
      roleCache.set(key, role);
    }
    return { job, role, skills: skillFit(job, prepared) };
  });
  const external = Array.isArray(profile.vector) && profile.vector.length === 384;
  const similarity = rows.map((row) => {
    if (!prepared.vector) return 0.5;
    const jobVector = external
      ? (Array.isArray(row.job.embedding) && row.job.embedding.length === 384 ? row.job.embedding : null)
      : vectorOf(row.job);
    if (!jobVector) return 0.5;
    return Math.max(0, Math.min(1, cosine(prepared.vector, jobVector)));
  });
  const top = new Set(similarity.map((value, index) => [value, index]).sort((a, b) => b[0] - a[0]).slice(0, 500).map((row) => row[1]));
  const ranked = rows.map((row, index) => {
    const candidate = row.role.weight > 0 || row.skills.requiredMatched >= 3 || top.has(index);
    const years = row.job.years_required == null ? null : Number(row.job.years_required);
    const exp = experienceFit(prepared.years, years);
    const fresh = freshness(row.job.posted_at, prepared.days, now);
    const level = seniorityFit(row.job.title, prepared.years);
    const raw = 100 * (0.35 * row.role.weight + 0.35 * row.skills.fit + 0.15 * exp + 0.1 * similarity[index] + 0.05 * fresh) * Math.sqrt(level);
    const score = Math.round(Math.max(0, Math.min(100, raw)));
    const tier = candidate ? tierOf(score) : "hide";
    return {
      ...row.job,
      match_score: score,
      tier,
      bucket: tier === "hide" ? "possible" : "match",
      relation: relationLabel(row.role),
      role_id: row.role.id || row.job.role_id || "",
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
