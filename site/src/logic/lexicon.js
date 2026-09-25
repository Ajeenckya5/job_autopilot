/**
 * The vocabulary of every field, learned from the postings by feed-builder/lexicon.py: skill phrases
 * with how rare each is, title words with how rare each is, and titles whose postings read alike.
 * There is no hand-written skill or role list; a new field shows up here as soon as its postings do.
 *
 * Words are split exactly as the builder splits them, so a phrase found here is the phrase it counted.
 */

const TOKEN = /[a-z0-9+#]+(?:[.&][a-z0-9+#]+)*/g;
const BREAK = /[,;:!?()[\]{}|•·–—"“”‘’*…]|\.(?=\s|$)|\s-\s|\n/;
const TITLE_BREAK = /\s[-–—|]\s|[,([:]/;
const EMPTY = Object.freeze({ docs: 0, phrases: {}, filler: [], title_idf: {}, titles: {} });

let current = EMPTY;
let fillerSet = new Set();
let loading = null;

export function setLexicon(lexicon) {
  current = lexicon && lexicon.phrases ? lexicon : EMPTY;
  fillerSet = new Set(current.filler || []);
  return current;
}

export function getLexicon() {
  return current;
}

export function hasLexicon() {
  return current.docs > 0;
}

/**
 * Load the lexicon that shipped with this build of the site. It is one JSON file, so the page and
 * the ranking worker share a single download, and it is kept in memory only.
 */
export function loadLexicon(env = globalThis) {
  if (hasLexicon()) return Promise.resolve(current);
  if (!loading) {
    loading = import("../../../feeds/lexicon.json?url")
      .then((module) => env.fetch(new URL(module.default, env.location ? env.location.href : "http://localhost/")))
      .then((response) => {
        if (!response.ok) throw new Error("vocabulary unavailable");
        return response.json();
      })
      .then((data) => setLexicon(data))
      .catch(() => {
        loading = null;
        return current;
      });
  }
  return loading;
}

export function fold(text) {
  return String(text || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[/\-_'’]/g, " ");
}

export function segments(text) {
  return fold(text)
    .split(BREAK)
    .filter((part) => part && part.trim())
    .map((part) => part.match(TOKEN) || [])
    .filter((words) => words.length);
}

export function isWord(token) {
  return token.length >= 2 && !/^[0-9.]+$/.test(token);
}

export function isFiller(token, lexicon = current) {
  return lexicon === current ? fillerSet.has(token) : (lexicon.filler || []).includes(token);
}

/**
 * Every skill phrase in a text. Longer phrases win where they overlap ("machine learning" over
 * "learning"); each phrase comes back once with how often it appeared and where it first did.
 * `all` also lists the shorter phrases inside the longer ones.
 */
export function phrasesIn(text, lexicon = current, { all = false } = {}) {
  const table = lexicon.phrases || {};
  const found = new Map();
  let position = 0;
  segments(text).forEach((words) => {
    const spans = [];
    for (let i = 0; i < words.length; i += 1) {
      for (let n = 3; n >= 1; n -= 1) {
        if (i + n > words.length) continue;
        const phrase = n === 1 ? words[i] : words.slice(i, i + n).join(" ");
        if (table[phrase] !== undefined) spans.push({ start: i, end: i + n, phrase });
      }
    }
    spans.forEach((span) => {
      const inside = spans.some((other) => other !== span
        && other.start <= span.start && other.end >= span.end
        && other.end - other.start > span.end - span.start);
      if (inside && !all) return;
      const row = found.get(span.phrase);
      if (row) {
        row.count += 1;
        if (!inside) row.whole = true;
      } else {
        found.set(span.phrase, {
          phrase: span.phrase,
          idf: table[span.phrase],
          count: 1,
          first: position + span.start,
          whole: !inside,
        });
      }
    });
    position += words.length;
  });
  return [...found.values()];
}

/** The phrases of a text as a Set, including the shorter ones inside longer ones. */
export function phraseSet(text, lexicon = current) {
  return new Set(phrasesIn(text, lexicon, { all: true }).map((row) => row.phrase));
}

export function phraseIdf(phrase, lexicon = current) {
  const value = (lexicon.phrases || {})[phrase];
  return value === undefined ? 0 : value;
}

/**
 * A phrase that names a job rather than a skill: every word appears in titles and the last one
 * nearly always ends them ("software engineer", "paralegal"). Role fit reads titles; skills skip them.
 */
export function isTitlePhrase(phrase, lexicon = current) {
  const words = String(phrase || "").split(" ").filter(Boolean);
  const table = lexicon.title_idf || {};
  const heads = lexicon.heads || {};
  if (!words.length || !words.every((word) => table[word] !== undefined)) return false;
  return (heads[words[words.length - 1]] || 0) >= 0.5;
}

/** Title words, lowercased and split as the builder splits them. */
export function titleWords(title) {
  return segments(title).flat().filter(isWord);
}

/** The title before any comma, dash or bracket: "senior accountant" of "Senior Accountant - Remote". */
export function titleCore(title) {
  return titleWords(String(title || "").split(TITLE_BREAK)[0]);
}

/** How rare a title word is. Words never seen in a title count as rare as the rarest seen. */
export function titleIdf(word, lexicon = current) {
  const table = lexicon.title_idf || {};
  if (table[word] !== undefined) return table[word];
  return lexicon.docs ? Math.log(lexicon.docs) : 1;
}

/**
 * Titles related to one the person typed, from how alike their postings read. Looks up the typed
 * title's core and its endings ("staff accountant", then "accountant"), longest first.
 */
export function relatedTitles(title, lexicon = current) {
  const core = titleCore(title);
  const table = lexicon.titles || {};
  const out = new Map();
  for (let n = Math.min(3, core.length); n >= 1; n -= 1) {
    const term = core.slice(-n).join(" ");
    const rows = table[term];
    if (!rows) continue;
    const scale = n === core.length ? 1 : 0.9;
    rows.forEach(([other, sim]) => {
      const weight = Math.round(sim * scale * 100) / 100;
      if (!out.has(other) || out.get(other) < weight) out.set(other, weight);
    });
    if (out.size) break;
  }
  return [...out.entries()].map(([term, weight]) => ({ term, weight })).sort((a, b) => b.weight - a.weight);
}
