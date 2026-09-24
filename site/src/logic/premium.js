import { quoteFound } from "./llm/scorecard.js";

export const PLAN = "pro";
export const WHATS_NEW = "folio-1";

export function verifiedQuotes(job, resume) {
  const rows = job?.ai?.requirements || [];
  const quotes = [];
  rows.forEach((row) => {
    if ((row.status === "met" || row.status === "partial") && quoteFound(resume, row.evidence_quote)) {
      quotes.push(String(row.evidence_quote).trim());
    }
  });
  return [...new Set(quotes)];
}

export function applyKit(job, resume, name = "") {
  const title = job?.title || "this role";
  const company = job?.company || "the company";
  const who = String(name || "").trim() || "I";
  const quotes = verifiedQuotes(job, resume);
  const bullets = quotes.map((quote) => `Emphasize this line, which is already on the resume: “${quote}”`);
  const evidence = quotes.map((quote) => `My resume already says “${quote}”.`);
  const why = quotes.length
    ? `${who} can point to ${quotes.length} ${quotes.length === 1 ? "line" : "lines"} already on the resume for ${title} at ${company}.`
    : "No verified resume line is ready for this posting yet. Score the job before drafting.";
  const cover = [
    "Hello,",
    "",
    `I am writing about the ${title} role at ${company}.`,
    ...(evidence.length ? evidence : ["I am not adding experience that is not already on my resume."]),
    "",
    "Thank you,",
    who,
  ].join("\n");
  const referral = quotes.length
    ? `I am interested in ${title} at ${company}. A line already on my resume: “${quotes[0]}”.`
    : `I am interested in ${title} at ${company}. I will not describe experience that is not on my resume.`;
  return { bullets, why, cover, referral };
}

export function interviewPrep(job, resume) {
  const rows = job?.ai?.requirements || [];
  if (!rows.length) return [];
  return rows.slice(0, 8).map((row) => {
    const story = quoteFound(resume, row.evidence_quote) ? String(row.evidence_quote).trim() : "";
    return {
      question: `How have you done this: ${row.text}?`,
      story,
    };
  });
}

export function followUpDraft(job, days) {
  const title = job?.title || "the role";
  const company = job?.company || "your team";
  const applied = job?.applied_at ? `on ${String(job.applied_at).slice(0, 10)}` : "recently";
  return `Hello,\n\nI applied for ${title} at ${company} ${applied}. It has been ${days} days, and I wanted to ask if the application is still open.\n`;
}

export const SHORTCUTS = [
  ["J / K", "Move between roles"],
  ["S", "Save the selected role"],
  ["A", "Mark the selected role applied"],
  ["H", "Hide the selected role"],
  ["N", "Not relevant"],
  ["/", "Filter roles"],
  ["G then T", "Open the tracker"],
  ["?", "Show these shortcuts"],
  ["Cmd or Ctrl + K", "Command palette"],
];
