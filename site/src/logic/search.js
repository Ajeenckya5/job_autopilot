import { apiBase } from "./sync.js";
import { fold, isTitlePhrase, phraseIdf, titleCore, titleIdf } from "./lexicon.js";
import { resumePhrases, skillsFromText } from "./resume.js";
import { familyMap } from "./match.js";
import { clampLookback, htmlToText } from "./text.js";

export const JOBS_API = "https://jobs-api.ajeenckyam8.workers.dev";
export const SEARCH_LIMIT = 300;

/** Text as the API stores it for search: folded like the lexicon, one space between words, a
 *  full stop kept only inside a word ("node.js"). The API matches whole words, so "lean" does not
 *  find "clean" and short titles such as "rn" are safe to send. */
export function searchable(text) {
  return fold(text).replace(/\.(?![a-z0-9])/g, " ").replace(/[^a-z0-9+#.& ]+/g, " ").replace(/\s+/g, " ").trim();
}

export function countriesFor(locations) {
  const found = new Set();
  const places = locations && locations.length ? locations : ["United States"];
  places.forEach((place) => {
    const blob = String(place || "");
    if (/germany|india|united kingdom|\buk\b|france|brazil|nigeria|singapore/i.test(blob)) {
      found.add("other");
      return;
    }
    if (/remote/i.test(blob)) found.add("remote");
    if (/united states|\busa\b|u\.s\./i.test(blob) || !/remote/i.test(blob)) found.add("united-states");
  });
  if (!found.size) found.add("united-states");
  return [...found].slice(0, 4);
}

/**
 * Title phrases the API searches first: what the person typed, titles they have held, then titles
 * whose postings read alike, closest first. Short ones ("ML") are ranked here instead, since they
 * match inside words.
 */
export function searchTermsFor(profile, max = 16) {
  const terms = [];
  const add = (value) => {
    const term = searchable(value);
    if (term.length >= 2 && term.length <= 40 && !terms.includes(term)) terms.push(term);
  };
  const entries = [...familyMap(profile || {}).values()];
  const order = { target: 0, resume: 1, related: 2 };
  entries
    .filter((entry) => entry.kind !== "related" || entry.weight >= 0.55)
    .sort((a, b) => order[a.kind] - order[b.kind] || b.weight - a.weight)
    .forEach((entry) => add(entry.kind === "related" ? entry.label : entry.label.replace(/[,()|].*$/, "")));
  // The last word of a typed title when it is rare among titles ("nurse", "accountant", not "manager").
  (profile && profile.roles ? profile.roles : []).forEach((role) => {
    const head = titleCore(role).slice(-1)[0];
    if (head && titleIdf(head) >= 4) add(head);
  });
  return terms.slice(0, max);
}

/**
 * The resume's rarest skills, as the person wrote them or as postings spell them, which the API
 * uses to put postings that ask for them ahead of the rest once titles run out. Job titles and
 * repeats inside longer skills are left out. Only these phrases, never the resume, leave the device.
 */
export function skillTermsFor(profile, max = 12) {
  const source = profile || {};
  const listed = [...(source.skills || []), ...skillsFromText(source.resume_text || "", 40)];
  const rows = [];
  const seen = new Set();
  const push = (value, rank) => {
    const phrase = searchable(value);
    if (phrase.length < 2 || phrase.length > 40 || seen.has(phrase) || isTitlePhrase(phrase)) return;
    seen.add(phrase);
    rows.push({ phrase, rank, idf: phraseIdf(phrase) || 5 });
  };
  // What the person lists comes first; phrases from the rest of the resume fill the remainder.
  listed.forEach((skill) => push(skill, 0));
  resumePhrases(source.resume_text || "").forEach((row) => push(row.phrase, 1));
  // "six sigma" finds every posting "six sigma green belt" would, so the shorter one is kept.
  const kept = rows.filter((row) => !rows.some((other) => other !== row
    && other.phrase.length < row.phrase.length && ` ${row.phrase} `.includes(` ${other.phrase} `)));
  return kept.sort((a, b) => a.rank - b.rank || b.idf - a.idf).slice(0, max).map((row) => row.phrase);
}

export function searchQuery(profile, now = Date.now()) {
  const days = clampLookback(profile && profile.lookback_days);
  return {
    countries: countriesFor(profile && profile.locations),
    terms: searchTermsFor(profile),
    skills: skillTermsFor(profile),
    since: new Date(now - days * 86400000).toISOString(),
    limit: SEARCH_LIMIT,
  };
}

function jobsApi() {
  return apiBase() || JOBS_API;
}

export async function fetchCandidates(profile, env = globalThis) {
  const query = searchQuery(profile);
  const url = new URL(`${jobsApi()}/v1/search`);
  query.countries.forEach((country) => url.searchParams.append("country", country));
  query.terms.forEach((term) => url.searchParams.append("term", term));
  query.skills.forEach((skill) => url.searchParams.append("skill", skill));
  url.searchParams.set("since", query.since);
  url.searchParams.set("limit", String(SEARCH_LIMIT));
  const response = await env.fetch(url, { mode: "cors" });
  if (!response.ok) throw new Error("Search is unavailable.");
  const body = await response.json();
  const jobs = (body.jobs || []).slice(0, SEARCH_LIMIT).map((job) => ({
    ...job,
    description_text: htmlToText(job.description_text),
  }));
  jobs.titleMatches = Math.min(jobs.length, Number(body.title_matches) || 0);
  return jobs;
}

export async function fetchJobText(job, env = globalThis) {
  const base = jobsApi();
  try {
    const response = await env.fetch(`${base}/v1/job/${encodeURIComponent(job?.id || "")}`);
    if (!response.ok) return "";
    const body = await response.json();
    return htmlToText(body.description_text);
  } catch (_) {
    return "";
  }
}

export async function fetchProfileVector(profile, env = globalThis) {
  const body = {
    skills: (profile.skills || []).slice(0, 24),
    roles: (profile.roles || []).slice(0, 8),
  };
  const response = await env.fetch(`${jobsApi()}/v1/profile-vector`, {
    method: "POST",
    mode: "cors",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) return null;
  const data = await response.json();
  if (!Array.isArray(data.vector) || data.vector.length !== 384) return null;
  return data.vector;
}
