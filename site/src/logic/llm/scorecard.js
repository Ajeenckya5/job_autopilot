import { yearsFromResume } from "../match.js";

export const RESULT_LIMIT = 2 * 1024;
export const CACHE_LIMIT = 500;

export function normalizeSpace(text) {
  return String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
}

export function quoteFound(resume, quote) {
  const needle = normalizeSpace(quote);
  if (!needle) return false;
  return normalizeSpace(resume).includes(needle);
}

export function verifyRequirements(requirements, resume) {
  let checked = 0;
  let valid = 0;
  const next = (requirements || []).map((row) => {
    if (row.status !== "met" && row.status !== "partial") return row;
    checked += 1;
    if (quoteFound(resume, row.evidence_quote)) {
      valid += 1;
      return row;
    }
    if (row.status === "met") return { ...row, status: "partial" };
    return { ...row, status: "missing", evidence_quote: "" };
  });
  return { requirements: next, checked, valid, rate: checked ? valid / checked : 1 };
}

function coverage(rows) {
  if (!rows.length) return 0;
  const sum = rows.reduce((total, row) => total + (row.status === "met" ? 1 : row.status === "partial" ? 0.5 : 0), 0);
  return sum / rows.length;
}

function experienceFit(have, required) {
  if (required == null || required === "" || !Number.isFinite(Number(required))) return 0.8;
  const gap = have - Number(required);
  if (gap >= -1 && gap <= 3) return 1;
  const beyond = gap < -1 ? -1 - gap : gap - 3;
  return Math.max(0, 1 - beyond * 0.2);
}

function seniorityFit(value) {
  if (value === "match") return 1;
  if (value === "under") return 0.5;
  if (value === "over") return 0.35;
  return 0.5;
}

export function dealbreakerConflicts(dealbreakers, profile) {
  const source = profile || {};
  const blob = `${source.resume_text || ""} ${(source.skills || []).join(" ")}`;
  const places = (source.locations || []).join(" ");
  return (dealbreakers || []).some((item) => {
    const text = String(item || "");
    if (/no visa sponsorship|not sponsor|without sponsorship|no sponsorship/i.test(text) && source.sponsorship_needed) return true;
    if (/security clearance/i.test(text) && !/clearance/i.test(blob)) return true;
    if (/citizenship|must be a (?:us |u\.s\. )?citizen/i.test(text) && !/citizen/i.test(blob)) return true;
    if (/on-site in another country|onsite in another country|relocate abroad/i.test(text) && !/india|germany|united kingdom|france|brazil|nigeria|singapore/i.test(places)) return true;
    return false;
  });
}

export function codeScore(result, profile, resumeText) {
  const rows = result?.requirements || [];
  const must = coverage(rows.filter((row) => row.type !== "nice"));
  const nice = coverage(rows.filter((row) => row.type === "nice"));
  const have = yearsFromResume(resumeText || profile?.resume_text || "");
  const exp = experienceFit(have, result?.years_required);
  const role = Math.max(0, Math.min(1, Number(result?.role_fit) || 0));
  const fit = 0.5 * role + 0.5 * seniorityFit(result?.seniority_fit);
  let score = 100 * (0.6 * must + 0.15 * nice + 0.15 * exp + 0.1 * fit);
  if (dealbreakerConflicts(result?.dealbreakers, profile)) score = Math.min(score, 40);
  return Math.max(0, Math.min(100, Math.round(score)));
}

export function applyScore(raw, profile, resumeText) {
  const verified = verifyRequirements(raw.requirements, resumeText);
  const candidateYears = yearsFromResume(resumeText || "");
  const result = {
    ...raw,
    requirements: verified.requirements,
    candidate_years: candidateYears,
    code_score: 0,
    llm_overall: raw.llm_overall,
  };
  result.code_score = codeScore(result, profile, resumeText);
  result.needs_look = Math.abs(result.code_score - Number(result.llm_overall)) > 25;
  result.evidence_rate = verified.rate;
  return result;
}

function words(text, max) {
  return String(text || "").trim().split(/\s+/).filter(Boolean).slice(0, max).join(" ");
}

export function compactResult(result) {
  const next = {
    job_id: String(result?.job_id || ""),
    requirements: (result?.requirements || []).slice(0, 12).map((row) => ({
      text: words(row.text, 16),
      type: row.type === "nice" ? "nice" : "must",
      status: row.status,
      evidence_quote: words(row.evidence_quote, 25),
      note: words(row.note, 15),
    })),
    years_required: result?.years_required == null ? null : Number(result.years_required),
    candidate_years: Number(result?.candidate_years) || 0,
    seniority_fit: result?.seniority_fit || "match",
    role_fit: Number(result?.role_fit) || 0,
    dealbreakers: (result?.dealbreakers || []).slice(0, 4).map((item) => words(item, 12)),
    llm_overall: Number(result?.llm_overall) || 0,
    code_score: Number(result?.code_score) || 0,
    summary: words(result?.summary, 30),
    needs_look: !!result?.needs_look,
    evidence_rate: Number.isFinite(Number(result?.evidence_rate)) ? Number(result.evidence_rate) : 1,
  };
  while (JSON.stringify(next).length > RESULT_LIMIT && next.requirements.length) next.requirements.pop();
  if (JSON.stringify(next).length > RESULT_LIMIT) next.summary = words(next.summary, 12);
  return next;
}

export function positioningTips(result, resume) {
  const quotes = [];
  (result?.requirements || []).forEach((row) => {
    if ((row.status === "met" || row.status === "partial") && quoteFound(resume, row.evidence_quote)) {
      quotes.push(row.evidence_quote);
    }
  });
  const unique = [...new Set(quotes)].slice(0, 5);
  if (!unique.length) return ["Nothing already on the resume is ready to emphasize for this posting."];
  return unique.map((quote) => `Emphasize this line, which is already on the resume: “${quote}”`);
}
