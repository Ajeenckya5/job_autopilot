import { apiBase } from "./sync.js";
import { familyMap } from "./match.js";
import { clampLookback, htmlToText } from "./text.js";

export const JOBS_API = "https://jobs-api.ajeenckyam8.workers.dev";
export const SEARCH_LIMIT = 300;

export function familyOf(text) {
  const blob = String(text || "").toLowerCase();
  if (/nurse|clinic|health|patient|pharma/.test(blob)) return "health";
  if (/driver|warehouse|logistic|supply/.test(blob)) return "logistics";
  if (/retail|cashier|sales associate/.test(blob)) return "retail";
  if (/engineer|software|data|design|product/.test(blob)) return "software";
  return "general";
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

export function familiesFor(roles) {
  const found = new Set((roles && roles.length ? roles : [""]).map((role) => familyOf(role)));
  return [...found].slice(0, 4);
}

/**
 * Title phrases the API searches first: what the person typed, then each related role's title and
 * synonyms, closest first. Short ones ("ML") are ranked here instead, since they match inside words.
 */
export function searchTermsFor(profile, max = 16) {
  const source = profile || {};
  const terms = [];
  const add = (value) => {
    const term = String(value || "").toLowerCase().replace(/\s+/g, " ").trim();
    if (term.length >= 4 && term.length <= 40 && !terms.includes(term)) terms.push(term);
  };
  (source.roles || []).forEach(add);
  [...familyMap(source).values()]
    .filter((entry) => entry.weight >= 0.6)
    .sort((a, b) => b.weight - a.weight)
    .forEach((entry) => {
      add(entry.role.title);
      (entry.role.synonyms || []).forEach(add);
    });
  return terms.slice(0, max);
}

export function searchQuery(profile, now = Date.now()) {
  const days = clampLookback(profile && profile.lookback_days);
  return {
    countries: countriesFor(profile && profile.locations),
    families: familiesFor(profile && profile.roles),
    terms: searchTermsFor(profile),
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
  query.families.forEach((family) => url.searchParams.append("family", family));
  query.terms.forEach((term) => url.searchParams.append("term", term));
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
