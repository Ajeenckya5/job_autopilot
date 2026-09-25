import { getLexicon, isTitlePhrase, isWord, phrasesIn, segments } from "./lexicon.js";

/**
 * Skills and past titles come from the resume itself, read against the vocabulary learned from the
 * postings (lexicon.js). There is no built-in list of skills or job titles.
 */

const MONTH = "(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\\.?\\s+";
const DATE_RANGE = new RegExp(`\\b(?:${MONTH})?(?:19|20)\\d{2}\\s*(?:[-–—]|to)\\s*(?:(?:${MONTH})?(?:19|20)\\d{2}|present|current|now|today)\\b`, "i");
const LIST_SPLIT = /\s*(?:,|;|\||•|·|\s\/\s)\s*/;
const TITLE_SPLIT = /\s+(?:[-–—|]|at|@|with)\s+|[,;()]|\t|\s{3,}/i;

/** Lines of the resume. Text read before line breaks were kept is cut at dates and bullets instead. */
export function resumeLines(text) {
  const raw = String(text || "");
  const lines = raw.split(/\r?\n/).map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
  if (lines.length >= 6) return lines;
  const dates = new RegExp(DATE_RANGE.source, "gi");
  return raw
    .replace(dates, (match) => `\n${match}\n`)
    .replace(/\s[•·▪]\s/g, "\n")
    .split(/\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
}

/** The original spelling of a folded phrase in a text: "civil 3d" → "Civil 3D". */
export function displayForm(phrase, text, lower) {
  const words = String(phrase || "").split(" ").filter(Boolean);
  if (!words.length) return "";
  const source = String(text || "");
  const low = lower || source.toLowerCase();
  const at = low.indexOf(phrase);
  const before = at > 0 ? low.charCodeAt(at - 1) : 32;
  const after = at >= 0 ? low.charCodeAt(at + phrase.length) || 32 : 32;
  const edge = (code) => !((code >= 97 && code <= 122) || (code >= 48 && code <= 57) || code === 43 || code === 35);
  if (at >= 0 && edge(before) && edge(after)) return source.slice(at, at + phrase.length);
  const pattern = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[\\s\\-/'’_]+");
  const hit = new RegExp(`(?:^|[^A-Za-z0-9+#])(${pattern})(?![A-Za-z0-9+#])`, "i").exec(String(text || ""));
  return hit ? hit[1] : phrase;
}

const BULLET = /^[-–•*·▪►‣◦]\s*/;

function listedItems(lines) {
  const items = [];
  lines.forEach((line) => {
    if (DATE_RANGE.test(line)) return;
    const colon = line.indexOf(":");
    const labelled = colon > 0 && colon < 40;
    const body = labelled ? line.slice(colon + 1) : line.replace(BULLET, "");
    const parts = body.split(LIST_SPLIT).map((part) => part.trim()).filter(Boolean);
    // A labelled list ("Skills: ...") or four or more short items is a list the person wrote:
    // skills, tools, licenses, languages. "Employer, City, ST" is not.
    if (parts.length < (labelled ? 2 : 4)) return;
    const short = parts.filter((part) => part.split(/\s+/).length <= 4 && part.length <= 40
      && !/\d{3,}|@|https?:|www\./i.test(part) && /[A-Za-z]{2}/.test(part) && !/^[A-Z]{2}$/.test(part));
    if (short.length >= 2 && short.length >= parts.length * (labelled ? 0.75 : 1)) items.push(...short);
  });
  return items;
}

/** A line of names only: "Northside Hospital, Atlanta, GA" or "Journeyman Electrician". */
function nameLine(line) {
  const parts = line.split(/[,|;]|\s[-–—]\s/).map((part) => part.trim()).filter(Boolean);
  return parts.length > 0 && parts.every((part) => {
    const words = part.split(/\s+/);
    return words.length <= 5 && words.every((word) => /^[A-Z0-9&(][\w&.'’()/-]*$/.test(word));
  });
}

/** Lines that describe work, without the name at the top and lines that only name an employer,
 *  a title or a place. */
function bodyText(lines) {
  return lines
    .filter((line, index) => index > 0 && (BULLET.test(line) || !nameLine(line)))
    .map((line) => line.replace(/^[^:]{1,40}:/, " "))
    .join("\n");
}

/** Words of the person's name, from a first line of two to four capitalised words. */
function nameWords(text) {
  const first = String(text || "").trim().split(/\n/)[0] || "";
  const hit = /^([A-Z][a-z'.-]+(?:\s+[A-Z][a-z'.-]+){1,3})(?=$|[\s,|])/.exec(first.trim());
  if (!hit) return new Set();
  const words = hit[1].split(/\s+/);
  const named = first.trim() === hit[1] ? words : words.slice(0, 2);
  return new Set(named.map((word) => segments(word).flat().join(" ")).filter(Boolean));
}

/**
 * Skills in a resume: the items the person listed, then every phrase the postings use as a skill,
 * rarest first. Returns the resume's own spelling, deduplicated, at most `limit`.
 */
export function skillsFromText(text, limit = 24, lexicon = getLexicon()) {
  const source = String(text || "");
  const seen = new Set();
  const words = new Set();
  const out = [];
  const add = (label) => {
    const clean = String(label || "").replace(/\s+/g, " ").trim();
    const key = segments(clean).flat().join(" ");
    if (!clean || !key || seen.has(key)) return;
    // "Lesson" adds nothing next to "lesson planning".
    if (!key.includes(" ") && words.has(key)) return;
    seen.add(key);
    key.split(" ").forEach((word) => words.add(word));
    out.push(clean);
  };
  const lines = resumeLines(source);
  const name = nameWords(source);
  listedItems(lines).forEach(add);
  const weight = (row) => row.idf * (row.phrase.includes(" ") ? 1.5 : 1) * (1 + Math.log(row.count));
  phrasesIn(lines.length > 1 ? bodyText(lines) : source, lexicon)
    .filter((row) => !isTitlePhrase(row.phrase, lexicon) && !row.phrase.split(" ").some((word) => name.has(word)))
    .filter((row) => row.phrase.includes(" ") || row.count >= 2 || namedLikeATool(displayForm(row.phrase, source), source))
    .sort((a, b) => weight(b) - weight(a) || a.first - b.first)
    .forEach((row) => add(displayForm(row.phrase, source)));
  return limit == null ? out : out.slice(0, limit);
}

/**
 * A single word said once counts as a skill when it is written like a name: all capitals ("IEP"),
 * capitals inside ("PyTorch"), or a capital in mid-sentence ("in Epic"). "wrote" does not.
 */
function namedLikeATool(label, text) {
  if (/^[A-Z0-9+#&.]{2,}$/.test(label) || /[a-z][A-Z]/.test(label)) return true;
  if (!/^[A-Z]/.test(label)) return false;
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`[a-z0-9,;&/] +${escaped}(?![A-Za-z0-9])`).test(text);
}

export function allSkillsIn(text, lexicon = getLexicon()) {
  return skillsFromText(text, null, lexicon);
}

/** A piece of a line is a job title when nearly all its words appear in titles and its last word
 *  is one that ends titles ("accountant", "driver"), not one that names an employer or a field. */
function titleScore(words, lexicon) {
  const table = lexicon.title_idf || {};
  const heads = lexicon.heads || {};
  const known = words.filter((word) => table[word] !== undefined);
  if (!words.length || !known.length || known.length / words.length < 0.75) return 0;
  const head = heads[words[words.length - 1]] || 0;
  // One word alone ("Education", "Operations") must almost always end a title to count as one.
  if (head < (words.length === 1 ? 0.5 : 0.1)) return 0;
  if (words.length === 1 && words[0].length < 3) return 0;
  return known.length / words.length;
}

function tidyTitle(label) {
  const text = String(label || "").trim();
  if (text === text.toUpperCase() && /[A-Z]{4}/.test(text)) {
    return text.toLowerCase().replace(/\b[a-z]/g, (ch) => ch.toUpperCase());
  }
  return text;
}

/**
 * Job titles the person has held, from the lines around the dates in their work history and a
 * headline at the top. A piece of a line counts when nearly all its words appear in job titles.
 */
export function titlesFromResume(text, lexicon = getLexicon(), limit = 6) {
  if (!lexicon.title_idf) return [];
  const lines = resumeLines(text);
  const near = new Set();
  lines.forEach((line, index) => {
    if (DATE_RANGE.test(line)) for (let k = index - 2; k <= index + 2; k += 1) near.add(k);
  });
  const found = [];
  const seen = new Set();
  lines.forEach((line, index) => {
    if ((!near.has(index) && index > 3) || line.length > 120 || BULLET.test(line) || /^[^:]{1,40}:/.test(line)) return;
    line.replace(new RegExp(DATE_RANGE.source, "gi"), " ").split(TITLE_SPLIT).forEach((part) => {
      const label = String(part || "").replace(/\s+/g, " ").trim();
      const words = segments(label).flat().filter(isWord);
      if (!words.length || words.length > 6 || titleScore(words, lexicon) < 0.75) return;
      const key = words.join(" ");
      if (seen.has(key)) return;
      seen.add(key);
      found.push({ label, near: near.has(index) });
    });
  });
  const ordered = found.filter((row) => row.near).concat(found.filter((row) => !row.near));
  return ordered.slice(0, limit).map((row) => tidyTitle(row.label));
}

/** Titles to search for, from the resume. Empty when it names none: nothing is guessed. */
export function suggestTitles(text, lexicon = getLexicon()) {
  return titlesFromResume(text, lexicon, 3);
}

export function guessName(text) {
  const line = String(text || "").split(/\n/).map((s) => s.trim()).find((s) => s.length > 3 && s.length < 60);
  if (!line) return "";
  if (/resume|curriculum|objective|summary|experience/i.test(line)) return "";
  const parts = line.split(/\s+/);
  if (parts.length < 2 || parts.length > 4) return "";
  if (!parts.every((p) => /^[A-Z][a-z'.-]+$/.test(p))) return "";
  return line;
}

/** Keep line breaks: they separate titles, employers, dates and skill lists. */
export function tidyText(text) {
  return String(text || "")
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t\f\v\u00a0]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function pdfPageText(items) {
  let out = "";
  items.forEach((item) => {
    out += item.str || "";
    out += item.hasEOL ? "\n" : " ";
  });
  return out;
}

export async function readResumeFile(file, deps = {}) {
  if (!file) throw new Error("Choose a resume file.");
  const name = file.name || "resume";
  const lower = name.toLowerCase();
  if (lower.endsWith(".doc") && !lower.endsWith(".docx")) {
    throw new Error("This .doc file can’t be read. Save it as PDF or DOCX and try again.");
  }
  if (lower.endsWith(".docx")) {
    const mammoth = deps.mammoth;
    if (!mammoth) throw new Error("DOCX reader failed to load. Try a PDF.");
    const raw = await file.arrayBuffer();
    const bytes = raw instanceof Uint8Array ? raw : new Uint8Array(raw);
    const arrayBuffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    const result = await mammoth.extractRawText({ buffer: bytes, arrayBuffer });
    const text = tidyText(result.value);
    if (text.length < 40) throw new Error("That DOCX had almost no text. Try a PDF export.");
    return { name, text, skills: skillsFromText(text) };
  }
  if (lower.endsWith(".pdf") || file.type === "application/pdf") {
    const buf = await file.arrayBuffer();
    const pdfjs = deps.pdfjs;
    if (!pdfjs) throw new Error("PDF reader failed to load. Refresh and try again.");
    const doc = await pdfjs.getDocument({ data: buf }).promise;
    const pages = Math.min(doc.numPages || 0, 6);
    const chunks = [];
    for (let i = 1; i <= pages; i += 1) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent();
      chunks.push(pdfPageText(content.items));
    }
    const text = tidyText(chunks.join("\n"));
    if (text.length < 40) {
      throw new Error("This PDF has no text layer. Export a text PDF, not a scan.");
    }
    return { name, text, skills: skillsFromText(text) };
  }
  const text = tidyText(await file.text());
  if (text.length < 40) throw new Error("That file has too little text to score against.");
  return { name, text, skills: skillsFromText(text) };
}
