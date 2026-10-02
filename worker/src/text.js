/**
 * Text as search stores it: accents dropped, lowercase, a one-letter prefix joined ("e-discovery"
 * is "ediscovery"), other hyphens and slashes as spaces, one space between words. The site folds
 * search terms the same way (site/src/logic/search.js), so a posting and a resume spell alike.
 */
export function searchable(text) {
  return String(text || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/\b([a-z0-9])-(?=[a-z0-9])/g, "$1")
    .replace(/[/\-_'’]/g, " ")
    .replace(/\.(?![a-z0-9])/g, " ")
    .replace(/[^a-z0-9+#.& ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, code) => {
    const key = code.toLowerCase();
    if (key[0] === "#") {
      const n = key[1] === "x" ? parseInt(key.slice(2), 16) : parseInt(key.slice(1), 10);
      if (!Number.isFinite(n) || n < 1 || n > 0x10ffff) return match;
      return String.fromCodePoint(n);
    }
    return ENTITIES[key] ?? match;
  });
}

/** Job boards send HTML, sometimes escaped twice. Search, embeddings and the site want plain text. */
export function htmlToText(raw) {
  let text = String(raw || "");
  if (/[<&]/.test(text)) {
    for (let i = 0; i < 2 && /&(lt|gt|amp|quot|#\d+);/i.test(text); i += 1) text = decodeEntities(text);
    text = text
      .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ")
      .replace(/<(br|li|\/p|\/div|\/li|\/h[1-6]|\/tr|\/ul|\/ol)\b[^>]*>/gi, "\n")
      .replace(/<[^>]*>/g, " ");
    text = decodeEntities(text);
  }
  // Keep line breaks: the LLM and the requirements reader use them to find sections.
  return text.replace(/[^\S\n]+/g, " ").replace(/ ?\n\s*/g, "\n").trim();
}

export const US_STATES = [
  "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "IA", "ID", "IL", "IN", "KS",
  "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV",
  "NY", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
];
