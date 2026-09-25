import { hasLexicon } from "./lexicon.js";
import { yearsFromResume } from "./match.js";
import { skillsFromText, titlesFromResume } from "./resume.js";

export const PROFILE_LIMIT = 10 * 1024;
export const RESUME_TEXT_LIMIT = 20 * 1024;

export function profileFromResume(text) {
  return {
    skills: skillsFromText(text, 24),
    titles: titlesFromResume(text, undefined, 12),
    years: yearsFromResume(text),
  };
}

function clipList(values, count, length) {
  return (Array.isArray(values) ? values : [])
    .map((value) => String(value || "").replace(/\s+/g, " ").trim().slice(0, length))
    .filter(Boolean)
    .slice(0, count);
}

export function compactProfile(data) {
  const text = String((data || {}).resume_text || "").slice(0, RESUME_TEXT_LIMIT);
  const next = { ...(data || {}) };
  delete next.resume_text;
  delete next.resume_file;
  delete next.resume;
  next.skills = clipList(next.skills, 24, 48);
  next.titles = clipList(next.titles, 12, 80);
  next.roles = clipList(next.roles, 8, 80);
  next.locations = clipList(next.locations, 8, 80);
  if (next.years != null && next.years !== "") next.years = Number(next.years) || 0;
  if (next.resume_name) next.resume_name = String(next.resume_name).slice(0, 80);
  if (next.name) next.name = String(next.name).slice(0, 80);
  while (JSON.stringify(next).length > PROFILE_LIMIT && (next.titles.length || next.skills.length || next.roles.length)) {
    if (next.titles.length) next.titles.pop();
    else if (next.skills.length) next.skills.pop();
    else next.roles.pop();
  }
  if (text) next.resume_text = text;
  return next;
}

/**
 * Fill skills, titles and years from the resume when they are missing. Profiles saved before skills
 * were learned from postings get the learned ones added once, next to the ones already chosen.
 */
export function absorbResume(data) {
  const source = data || {};
  if (!source.resume_text) return compactProfile(source);
  const parsed = profileFromResume(source.resume_text);
  const upgrade = hasLexicon() && source.skills_from !== "postings";
  const merge = (mine, found) => [...new Set([...(mine || []), ...(found || [])])];
  const next = {
    ...source,
    skills: upgrade ? merge(source.skills, parsed.skills) : (source.skills && source.skills.length ? source.skills : parsed.skills),
    titles: upgrade ? merge(parsed.titles, source.titles) : (source.titles && source.titles.length ? source.titles : parsed.titles),
    years: source.years == null || source.years === "" ? parsed.years : source.years,
  };
  if (hasLexicon()) next.skills_from = "postings";
  return compactProfile(next);
}
