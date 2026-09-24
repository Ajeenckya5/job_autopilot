const STOP = new Set([
  "the", "and", "for", "with", "from", "this", "that", "your", "our", "you",
  "are", "was", "were", "will", "have", "has", "had", "job", "jobs", "role",
  "work", "team", "about", "into", "over", "under", "than", "then", "they",
  "building", "production", "systems", "experience", "several", "years",
  "senior", "manager", "using", "including", "across", "strong", "working",
  "based", "well", "more", "also", "such", "within", "other", "their",
  "been", "able", "help", "make", "like", "just", "very", "high", "new",
]);

export function tokens(text) {
  return String(text || "")
    .toLowerCase()
    .split(/[^a-z0-9+#.]+/)
    .filter((t) => t.length > 2 && !STOP.has(t));
}

export function fnv(text) {
  let h = 2166136261;
  const s = String(text);
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function embed(text) {
  const v = new Float64Array(384);
  const toks = tokens(text);
  const grams = toks.slice();
  for (let i = 0; i < toks.length - 1; i += 1) grams.push(`${toks[i]}_${toks[i + 1]}`);
  grams.forEach((t) => {
    const h = fnv(t);
    v[h % 384] += (h & 1) ? 1 : -1;
  });
  let n = 0;
  for (let i = 0; i < v.length; i += 1) n += v[i] * v[i];
  n = Math.sqrt(n) || 1;
  return Array.from(v, (x) => x / n);
}

export function cosine(a, b) {
  const n = Math.min(a.length, b.length);
  let s = 0;
  for (let i = 0; i < n; i += 1) s += a[i] * b[i];
  return s;
}

export function clampLookback(days) {
  const n = Number(days);
  if (!Number.isFinite(n)) return 14;
  return Math.max(1, Math.min(30, Math.round(n)));
}

export function parseRunsPerDay(raw) {
  const text = String(raw ?? "").trim();
  if (!/^\d+$/.test(text)) return { ok: false, error: "Enter a whole number from 0 to 24." };
  const n = Number(text);
  if (n > 24) return { ok: false, error: "Enter a whole number from 0 to 24." };
  return { ok: true, value: n };
}

export function safeHref(url) {
  try {
    const u = new URL(String(url || ""), "https://invalid.local");
    if (u.protocol !== "https:") return "";
    return u.href;
  } catch (_) {
    return "";
  }
}

export function plural(n, word) {
  const num = Number(n) || 0;
  return `${num} ${word}${num === 1 ? "" : "s"}`;
}
