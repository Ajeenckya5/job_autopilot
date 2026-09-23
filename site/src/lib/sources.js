export function canonicalUrl(url) {
  try {
    const parsed = new URL(String(url || ""));
    if (parsed.protocol !== "https:") return "";
    ["trk", "trackingId", "refId", "trackingid"].forEach((key) => parsed.searchParams.delete(key));
    [...parsed.searchParams.keys()].filter((key) => key.startsWith("utm_")).forEach((key) => parsed.searchParams.delete(key));
    parsed.hostname = parsed.hostname.toLowerCase();
    if (parsed.pathname.length > 1 && parsed.pathname.endsWith("/")) parsed.pathname = parsed.pathname.slice(0, -1);
    return parsed.toString();
  } catch (_) {
    return "";
  }
}

export function linkedInId(url) {
  const text = String(url || "");
  const view = text.match(/\/jobs\/view\/(\d+)/);
  if (view) return view[1];
  const current = text.match(/currentJobId=(\d+)/);
  return current ? current[1] : "";
}

function titleOverlap(a, b) {
  const left = new Set(String(a || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2));
  const right = String(b || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2);
  if (!left.size || !right.length) return 0;
  const hits = right.filter((word) => left.has(word)).length;
  return hits / Math.max(left.size, right.length);
}

export function mergeSources(groups) {
  const rows = [];
  (groups || []).forEach((group) => {
    (group.jobs || []).forEach((job) => rows.push({ ...job, source: job.source || group.source }));
  });
  const merged = [];
  rows.forEach((job) => {
    const url = canonicalUrl(job.url);
    const lid = job.external_ids?.linkedin || linkedInId(job.url);
    const found = merged.find((item) => {
      const sameId = lid && (item.external_ids?.linkedin === lid || linkedInId(item.url) === lid);
      const sameUrl = url && canonicalUrl(item.url) === url;
      const fuzzy = titleOverlap(item.title, job.title) >= 0.9
        && String(item.company || "").toLowerCase() === String(job.company || "").toLowerCase();
      return sameId || sameUrl || fuzzy;
    });
    if (!found) {
      merged.push({
        ...job,
        url: url || job.url,
        seen_on: [job.source].filter(Boolean),
        apply_options: job.url ? [{ source: job.source || "feed", url: url || job.url }] : [],
        external_ids: { ...(job.external_ids || {}), ...(lid ? { linkedin: lid } : {}) },
      });
      return;
    }
    if (job.source && !found.seen_on.includes(job.source)) found.seen_on.push(job.source);
    if (url && !found.apply_options.some((option) => option.url === url)) {
      found.apply_options.push({ source: job.source || "feed", url });
    }
    if (job.source === "greenhouse" || job.source === "lever" || job.source === "ashby") {
      found.url = url || found.url;
      found.description_text = job.description_text || found.description_text;
    }
    if (job.posted_at && (!found.posted_at || job.posted_at < found.posted_at)) found.posted_at = job.posted_at;
  });
  return merged;
}

export function planSerpQueries(roles, locations, usedThisMonth, limit = 250) {
  const cap = Math.floor(limit * 0.9);
  if (usedThisMonth >= cap) return [];
  const room = cap - usedThisMonth;
  const pairs = [];
  (roles || []).forEach((role) => {
    (locations || []).forEach((location) => {
      pairs.push({ q: role, location, engine: "google_jobs" });
    });
  });
  return pairs.slice(0, Math.min(room, pairs.length));
}

export function guestAllowed(state, enabled, now = Date.now()) {
  if (!enabled) return false;
  if (state?.blockedUntil && now < state.blockedUntil) return false;
  return true;
}

export function noteGuestResponse(status, now = Date.now()) {
  if ([429, 999, 401, 403].includes(Number(status))) {
    return { status: "blocked", blockedUntil: now + 86400000, message: "LinkedIn asked us to stop. Waiting 24 hours." };
  }
  return { status: "ok", blockedUntil: 0, message: "" };
}

export function parseAlertHtml(html) {
  const text = String(html || "");
  const jobs = [];
  const seen = new Set();
  const pattern = /https:\/\/[^"'<\s]+/g;
  let match = pattern.exec(text);
  while (match) {
    const url = canonicalUrl(match[0].replace(/&amp;/g, "&"));
    const id = linkedInId(url);
    const jobright = /jobright\.ai\/jobs\//.test(url);
    if ((id || jobright) && url && !seen.has(url)) {
      seen.add(url);
      jobs.push({
        id: id ? `li-${id}` : `jr-${jobs.length}`,
        source: id ? "linkedin" : "jobright",
        captured_via: "email",
        url,
        title: "",
        company: "",
        external_ids: id ? { linkedin: id } : {},
      });
    }
    match = pattern.exec(text);
  }
  return jobs;
}
