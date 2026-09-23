import { canonicalUrl } from "../lib/sources.js";
import { scoreAll } from "./match.js";
import { skillsFromText } from "./resume.js";
import { clampLookback } from "./text.js";

const US = /\b(al|ak|az|ar|ca|co|ct|dc|de|fl|ga|hi|ia|id|il|in|ks|ky|la|ma|md|me|mi|mn|mo|ms|mt|nc|nd|ne|nh|nj|nm|nv|ny|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|va|vt|wa|wi|wv|wy|usa|united states|remote)\b/i;

export function yearsRequired(title, description) {
  const text = `${title || ""} ${description || ""}`;
  const m = text.match(/(\d{1,2})\s*\+?\s*(?:years|yrs)\b/i);
  if (!m) return null;
  const n = Number(m[1]);
  return n >= 0 && n <= 40 ? n : null;
}

export function sponsorshipOf(text) {
  const t = String(text || "").toLowerCase();
  if (/unable to sponsor|cannot sponsor|no sponsorship|without sponsorship|not (?:able|eligible) to sponsor|must be authorized to work/.test(t)) {
    return "no";
  }
  if (/visa sponsorship|will sponsor|sponsorship available|h-1b/.test(t)) return "yes";
  return "unknown";
}

export function locationOk(jobLocations, wanted) {
  const wants = (wanted || []).map((w) => String(w || "").trim().toLowerCase()).filter(Boolean);
  if (!wants.length) return true;
  const blob = (jobLocations || []).join(" ").toLowerCase();
  return wants.some((w) => {
    if (w.includes("remote")) return /remote|anywhere|distributed|worldwide/.test(blob) || !blob;
    if (/^(united states|usa|us|u\.s\.|america)$/.test(w)) {
      if (!blob || /remote|anywhere|united states|\busa\b|\bu\.s\./.test(blob)) return true;
      return US.test(blob) && !/germany|india|united kingdom|\buk\b|france|brazil|nigeria|singapore only/.test(blob);
    }
    return !blob || blob.includes(w);
  });
}

export function withinLookback(posted, days, now = Date.now()) {
  if (!posted) return true;
  const t = new Date(posted).getTime();
  if (Number.isNaN(t)) return true;
  return now - t <= clampLookback(days) * 86400000 && t <= now + 86400000;
}

const SYNONYMS = [
  ["machine learning engineer", "ml engineer", "machine learning scientist"],
  ["data scientist", "applied scientist", "research scientist"],
  ["software engineer", "software developer", "backend engineer"],
  ["data engineer", "analytics engineer"],
  ["product manager", "product owner"],
  ["registered nurse", "nurse", "rn", "staff nurse"],
  ["devops engineer", "site reliability engineer"],
];

export function expandRoles(roles) {
  const phrases = new Set();
  (roles || []).forEach((role) => {
    const phrase = String(role || "").toLowerCase().trim();
    if (!phrase) return;
    phrases.add(phrase);
    SYNONYMS.forEach((group) => {
      if (group.includes(phrase)) group.forEach((item) => phrases.add(item));
    });
  });
  return [...phrases];
}

export function titleFits(title, roles) {
  const phrases = expandRoles(roles);
  if (!phrases.length) return true;
  return titleScore(title, phrases) >= 0.5;
}

const GENERIC_TITLE = new Set(["engineer", "developer", "scientist", "manager", "associate", "analyst", "designer", "specialist"]);

function roleTokens(phrase) {
  return String(phrase || "")
    .toLowerCase()
    .split(/[^a-z0-9+#.]+/)
    .filter((word) => word.length >= 2);
}

function containsPhrase(blob, phrase) {
  if (phrase.length <= 3) {
    return new RegExp(`(?:^|[^a-z0-9])${phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:[^a-z0-9]|$)`).test(blob);
  }
  return blob.includes(phrase);
}

export function titleScore(title, roles) {
  const blob = String(title || "").toLowerCase();
  let best = 0;
  (roles || []).forEach((role) => {
    const phrase = String(role || "").toLowerCase().trim();
    if (!phrase) return;
    if (containsPhrase(blob, phrase)) {
      best = 1;
      return;
    }
    const words = roleTokens(phrase);
    const distinctive = words.filter((word) => !GENERIC_TITLE.has(word));
    const needed = distinctive.length ? distinctive : words;
    if (!needed.length) return;
    const hits = needed.filter((word) => containsPhrase(blob, word)).length;
    best = Math.max(best, hits / needed.length);
  });
  return best;
}

export function recencyScore(posted, days, now = Date.now()) {
  if (!posted) return 0.45;
  const t = new Date(posted).getTime();
  if (Number.isNaN(t)) return 0.45;
  const age = Math.max(0, now - t) / 86400000;
  const span = clampLookback(days);
  return Math.max(0, 1 - age / span);
}

export function skillHits(resumeText, jobText) {
  const resume = new Set(skillsFromText(resumeText));
  const found = skillsFromText(jobText);
  return {
    matched: found.filter((skill) => resume.has(skill)).slice(0, 8),
    missing: found.filter((skill) => !resume.has(skill)).slice(0, 5),
  };
}

export function cleanCompany(name) {
  return String(name || "").replace(/\s+/g, " ").trim();
}

export function jobLocationText(job) {
  if (job.locations && job.locations.length) {
    return job.locations
      .map((row) => [row.city, row.region, row.country, row.remote].filter(Boolean).join(" "))
      .concat(job.location_raw || "");
  }
  return [job.location_raw || job.location || ""];
}

export function postingKey(job) {
  const company = cleanCompany(job.company).toLowerCase();
  const title = String(job.title || "").replace(/\s+/g, " ").trim().toLowerCase();
  const desc = String(job.description_text || "").replace(/\s+/g, " ").trim().toLowerCase().slice(0, 480);
  const url = canonicalUrl(job.url);
  if (desc.length >= 80) return `${job.source || ""}|${company}|${title}|${desc}`;
  if (url) return `url|${url}`;
  return `id|${job.id || ""}`;
}

function trackerRank(job) {
  return (job.status && job.status !== "new" ? 2 : 0) + (job.notes ? 1 : 0);
}

function joinLocations(left, right) {
  const parts = [];
  `${left || ""} · ${right || ""}`.split(" · ").forEach((part) => {
    const text = part.replace(/\s+/g, " ").trim();
    if (text && !parts.includes(text)) parts.push(text);
  });
  return parts.join(" · ");
}

export function collapsePostings(jobs) {
  const map = new Map();
  const order = [];
  (jobs || []).forEach((job) => {
    const key = postingKey(job);
    const prev = map.get(key);
    const company = cleanCompany(job.company);
    if (!prev) {
      map.set(key, { ...job, company });
      order.push(key);
      return;
    }
    const incoming = { ...job, company };
    const keep = trackerRank(incoming) > trackerRank(prev) ? incoming : prev;
    const other = keep === prev ? incoming : prev;
    keep.location_raw = joinLocations(keep.location_raw, other.location_raw);
    const seen = new Set();
    keep.locations = [...(keep.locations || []), ...(other.locations || [])].filter((row) => {
      const blob = JSON.stringify(row);
      if (seen.has(blob)) return false;
      seen.add(blob);
      return true;
    });
    keep.match_score = Math.max(keep.match_score || 0, other.match_score || 0, prev.match_score || 0);
    map.set(key, keep);
  });
  return order.map((key) => map.get(key));
}

function eligible(job, profile, now) {
  const days = clampLookback(profile.lookback_days);
  if (!locationOk(jobLocationText(job), profile.locations)) return false;
  if (!withinLookback(job.posted_at, days, now)) return false;
  const years = job.years_required == null ? yearsRequired(job.title, job.description_text) : job.years_required;
  if (profile.max_years && years && years > Number(profile.max_years)) return false;
  if (profile.sponsorship_needed && (job.sponsorship || sponsorshipOf(job.description_text)) === "no") return false;
  const remote = job.remote_type === "remote" || /remote/.test(job.location_raw || "");
  if (profile.remote_only && !remote) return false;
  if (profile.min_salary && job.salary_max && Number(job.salary_max) < Number(profile.min_salary)) return false;
  if (profile.skip_leadership && /\b(staff|principal|director|vice president|\bvp\b|head of)\b/i.test(job.title || "")) return false;
  if (titleScore(job.title, profile.downrank || []) >= 0.5) return false;
  return true;
}

export function searchPool(jobs, profile, now = Date.now()) {
  const places = profile || {};
  return (jobs || []).filter((job) => locationOk(jobLocationText(job), places.locations) && withinLookback(job.posted_at, 30, now));
}

export function rankJob(job, profile, now = Date.now()) {
  if (!eligible(job, profile || {}, now)) return null;
  const ranked = scoreAll([job], profile, now)[0];
  return ranked ? { ...ranked, company: cleanCompany(ranked.company) } : null;
}

export function selectJobs(jobs, profile, controls = {}, now = Date.now()) {
  const pool = (jobs || []).filter((job) => job.status !== "hidden");
  const ranked = rankAll(pool, profile || {}, now);
  const reasons = [];
  let rows = ranked;
  if (!controls.showPossible) {
    const next = rows.filter((job) => job.bucket !== "possible");
    if (rows.length && !next.length) {
      reasons.push({
        id: "possible",
        text: `${rows.length} ${rows.length === 1 ? "role is a possible match" : "roles are possible matches"}.`,
        fix: "show-possible",
      });
    }
    rows = next;
  }
  const days = clampLookback((profile || {}).lookback_days);
  const tooOld = pool.filter((job) => job.posted_at && !withinLookback(job.posted_at, days, now));
  if (!rows.length && tooOld.length) {
    reasons.push({
      id: "lookback",
      text: `${tooOld.length} ${tooOld.length === 1 ? "role" : "roles"} hidden by look-back of ${days} days`,
      fix: "widen",
    });
  }
  const queried = filterJobsSafe(rows, controls);
  if (rows.length && !queried.length) {
    const why = controls.q ? "that search" : `status ${controls.status}`;
    reasons.push({ id: "query", text: `${rows.length} roles hidden by ${why}.`, fix: "clear-query" });
  }
  rows = queried;
  if (Number(controls.minScore)) {
    const next = rows.filter((job) => (job.match_score || 0) >= Number(controls.minScore));
    if (rows.length && !next.length) {
      reasons.push({ id: "score", text: `${rows.length} roles hidden by minimum score ${controls.minScore}.`, fix: "clear-score" });
    }
    rows = next;
  }
  if (controls.remoteOnly) {
    const next = rows.filter((job) => job.remote_type === "remote" || /remote/i.test(job.location_raw || ""));
    if (rows.length && !next.length) {
      reasons.push({ id: "remote", text: `${rows.length} roles hidden by Remote only.`, fix: "clear-remote" });
    }
    rows = next;
  }
  if (controls.since) {
    const next = rows.filter((job) => String(job.posted_at || "") > controls.since);
    if (rows.length && !next.length) {
      reasons.push({ id: "since", text: `${rows.length} roles hidden by new since last visit.`, fix: "clear-since" });
    }
    rows = next;
  }
  rows = collapsePostings(rows);
  if (rows.length < 20 && days < 30) {
    const wider = selectJobs(jobs, { ...(profile || {}), lookback_days: 30 }, controls, now);
    const seen = new Set(rows.map((job) => postingKey(job)));
    const added = wider.rows.filter((job) => !seen.has(postingKey(job))).length;
    if (added > 0) {
      reasons.push({
        id: "widen-more",
        text: `Look back 30 days would add ${added} ${added === 1 ? "role" : "roles"}.`,
        fix: "widen",
        added,
      });
    }
  }
  return { rows, reasons };
}

function filterJobsSafe(rows, controls) {
  const status = controls.status || "all";
  const query = String(controls.q || "").trim().toLowerCase();
  return rows.filter((job) => {
    const state = job.status || "new";
    if (status !== "all" && state !== status) return false;
    if (!query) return true;
    return `${job.title || ""} ${job.company || ""}`.toLowerCase().includes(query);
  });
}

export function arrangeJobs(jobs, profile, controls = {}, now = Date.now()) {
  const reasons = [];
  let rows = (jobs || []).filter((job) => job && job.status !== "hidden");
  rows = rows.filter((job) => titleScore(job.title, (profile || {}).downrank || []) < 0.5);
  if (!controls.showPossible) rows = rows.filter((job) => job.bucket !== "possible");
  const days = clampLookback((profile || {}).lookback_days);
  rows = rows.filter((job) => withinLookback(job.posted_at, days, now));
  rows = filterJobsSafe(rows, controls);
  if (Number(controls.minScore)) {
    rows = rows.filter((job) => (job.match_score || 0) >= Number(controls.minScore));
  }
  if (controls.remoteOnly) {
    rows = rows.filter((job) => job.remote_type === "remote" || /remote/i.test(job.location_raw || ""));
  }
  if (controls.since) {
    rows = rows.filter((job) => String(job.posted_at || "") > controls.since);
  }
  rows = collapsePostings(rows);
  rows.sort((a, b) => (b.match_score || 0) - (a.match_score || 0) || String(b.posted_at).localeCompare(String(a.posted_at)));
  if (rows.length < 20 && days < 30) {
    const seen = new Set(rows.map((job) => postingKey(job)));
    const older = (jobs || []).filter((job) => job && job.status !== "hidden" && !withinLookback(job.posted_at, days, now) && withinLookback(job.posted_at, 30, now));
    const added = collapsePostings(older).filter((job) => !seen.has(postingKey(job))).length;
    if (added > 0) {
      reasons.push({
        id: "widen-more",
        text: `Look back 30 days would add ${added} ${added === 1 ? "role" : "roles"}.`,
        fix: "widen",
        added,
      });
    }
  }
  return { rows, reasons };
}

export function rankAll(jobs, profile, now = Date.now()) {
  const source = profile || {};
  const rows = [];
  (jobs || []).forEach((job) => {
    if (eligible(job, source, now)) rows.push(job);
  });
  return scoreAll(rows, source, now).map((job) => ({ ...job, company: cleanCompany(job.company) }));
}

export function mergeJobs(previous, incoming) {
  const map = new Map();
  (previous || []).forEach((job) => map.set(job.id, job));
  (incoming || []).forEach((job) => {
    const old = map.get(job.id);
    if (!old) {
      map.set(job.id, job);
      return;
    }
    map.set(job.id, {
      ...job,
      status: old.status || job.status || "new",
      notes: old.notes || "",
      applied_at: old.applied_at || "",
      saved_at: old.saved_at || "",
    });
  });
  return [...map.values()];
}

export function applySearchResult(previous, result) {
  if (!result || result.ok === false) return previous || [];
  return mergeJobs(previous || [], result.jobs || []);
}
