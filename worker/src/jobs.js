import catalog from "../catalog.json" with { type: "json" };

export const EVENT_NAMES = ["onboarding_step", "search_run", "apply_clicked"];

export const DEFAULT_CONFIG = {
  sync: true,
  push: true,
  events: true,
  insights: true,
  api: true,
  vapid_public_key: "",
  llm_models: {
    gemini: "gemini-2.5-flash",
    groq: "llama-3.3-70b-versatile",
    openai: "gpt-4.1-mini",
    anthropic: "claude-3-5-haiku-latest",
    openrouter: "google/gemini-2.5-flash",
    ollama: "",
  },
  llm_daily_cap: 100,
  llm_top_n: 30,
  llm_concurrency: {
    gemini: 1,
    groq: 2,
    openai: 2,
    anthropic: 1,
    openrouter: 2,
    ollama: 1,
  },
};

const PUBLIC_FIELDS = [
  "id", "source", "company", "title", "url", "location_raw", "locations",
  "posted_at", "updated_at", "description_text", "salary_min", "salary_max",
  "department", "remote_type", "country", "family",
];

export function weekStart(iso, now = Date.now()) {
  const d = new Date(iso || now);
  if (Number.isNaN(d.getTime())) return weekStart(new Date(now).toISOString(), now);
  const day = d.getUTCDay();
  d.setUTCDate(d.getUTCDate() - ((day + 6) % 7));
  d.setUTCHours(0, 0, 0, 0);
  return d.toISOString().slice(0, 10);
}

export function publicJob(job) {
  const row = {};
  PUBLIC_FIELDS.forEach((key) => {
    if (job && job[key] != null && job[key] !== "") row[key] = job[key];
  });
  row.id = String(row.id || "");
  row.company = String(row.company || "").replace(/\s+/g, " ").trim();
  row.title = String(row.title || "");
  row.country = row.country || countryOf(row.location_raw || "");
  row.family = row.family || familyOf(`${row.title} ${row.department || ""}`);
  row.updated_at = String(row.updated_at || row.posted_at || new Date().toISOString());
  row.posted_at = String(row.posted_at || row.updated_at);
  if (String(row.url || "").startsWith("http://")) row.url = `https://${String(row.url).slice(7)}`;
  return row;
}

export function countryOf(location) {
  const blob = String(location || "");
  if (/germany|india|united kingdom|\buk\b|france|brazil|nigeria|singapore/i.test(blob)) return "other";
  if (/\bremote\b/i.test(blob) && !/united states|usa|u\.s\./i.test(blob)) return "remote";
  if (/united states|\busa\b|u\.s\.|chicago|new york|california|texas|remote/i.test(blob)) return "united-states";
  return "other";
}

export function familyOf(text) {
  const blob = String(text || "").toLowerCase();
  if (/nurse|clinic|health|patient|pharma/.test(blob)) return "health";
  if (/driver|warehouse|logistic|supply/.test(blob)) return "logistics";
  if (/retail|cashier|sales associate/.test(blob)) return "retail";
  if (/engineer|software|data|design|product/.test(blob)) return "software";
  return "general";
}

export async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(text)));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function syncPushPayload() {
  return JSON.stringify({ type: "sync" });
}

export function eventName(bodyText) {
  const raw = String(bodyText || "");
  if (raw.length > 80) return { error: "too_large" };
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (_) {
    return { error: "invalid" };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { error: "invalid" };
  const keys = Object.keys(parsed);
  if (keys.length !== 1 || keys[0] !== "name") return { error: "invalid" };
  if (!EVENT_NAMES.includes(parsed.name)) return { error: "invalid" };
  return { name: parsed.name };
}

async function metaGet(db, key) {
  const row = await db.prepare("SELECT value FROM meta WHERE key = ?").bind(key).first();
  return row ? row.value : "";
}

async function metaSet(db, key, value) {
  await db.prepare(
    "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
  ).bind(key, String(value)).run();
}

export async function upsertJobs(db, jobs) {
  let cursor = Number(await metaGet(db, "cursor") || 0);
  const changed = [];
  for (const job of jobs || []) {
    const row = publicJob(job);
    if (!row.id || !String(row.url || "").startsWith("https://")) continue;
    const payload = JSON.stringify(row);
    const hash = await sha256(payload);
    const existing = await db.prepare("SELECT content_hash FROM jobs WHERE id = ?").bind(row.id).first();
    if (existing && existing.content_hash === hash) continue;
    const isNew = !existing;
    cursor += 1;
    await db.prepare(
      `INSERT INTO jobs (id, country, family, company, updated_at, cursor, content_hash, payload)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         country = excluded.country,
         family = excluded.family,
         company = excluded.company,
         updated_at = excluded.updated_at,
         cursor = excluded.cursor,
         content_hash = excluded.content_hash,
         payload = excluded.payload`,
    ).bind(row.id, row.country, row.family, row.company, row.updated_at, cursor, hash, payload).run();
    if (isNew) {
      const week = weekStart(row.posted_at);
      await db.prepare(
        `INSERT INTO company_weeks (company, week, new_jobs) VALUES (?, ?, 1)
         ON CONFLICT(company, week) DO UPDATE SET new_jobs = new_jobs + 1`,
      ).bind(row.company, week).run();
    }
    changed.push(row);
  }
  await metaSet(db, "cursor", cursor);
  return { changed, cursor: String(cursor) };
}

export async function jobsSince(db, since, country, family) {
  const cursor = Number(since || 0);
  const result = await db.prepare(
    `SELECT payload, cursor FROM jobs
     WHERE cursor > ?
       AND (? = '' OR country = ?)
       AND (? = '' OR family = ?)
     ORDER BY cursor
     LIMIT 200`,
  ).bind(cursor, country, country, family, family).all();
  const rows = result.results || [];
  const jobs = rows.map((row) => JSON.parse(row.payload));
  const next = rows.length ? String(rows[rows.length - 1].cursor) : String(cursor);
  return { jobs, cursor: next };
}

export async function recordEvent(db, name, now = Date.now()) {
  const week = weekStart(new Date(now).toISOString(), now);
  await db.prepare(
    `INSERT INTO funnel (week, name, count) VALUES (?, ?, 1)
     ON CONFLICT(week, name) DO UPDATE SET count = count + 1`,
  ).bind(week, name).run();
  return week;
}

export async function funnelRows(db) {
  const result = await db.prepare("SELECT week, name, count FROM funnel ORDER BY week, name").bind().all();
  return result.results || [];
}

export async function companyTrend(db, company, now = Date.now()) {
  const weeks = [];
  for (let i = 7; i >= 0; i -= 1) weeks.push(weekStart(new Date(now - i * 7 * 86400000).toISOString(), now));
  const result = await db.prepare(
    "SELECT week, new_jobs FROM company_weeks WHERE company = ? AND week >= ? ORDER BY week",
  ).bind(company, weeks[0]).all();
  const counts = new Map((result.results || []).map((row) => [row.week, row.new_jobs]));
  return weeks.map((week) => ({ week, new_jobs: Number(counts.get(week) || 0) }));
}

export function httpsUrl(url) {
  const text = String(url || "");
  if (text.startsWith("https://")) return text;
  if (text.startsWith("http://")) return `https://${text.slice(7)}`;
  return "";
}

export async function fetchBoard(company) {
  const token = company.token;
  const name = company.name || token;
  let data;
  if (company.source === "lever") {
    const res = await fetch(`https://api.lever.co/v0/postings/${token}?mode=json`, {
      headers: { "user-agent": "JobAutopilot/1.0", accept: "application/json" },
    });
    if (!res.ok) return [];
    data = await res.json();
    return (Array.isArray(data) ? data : []).slice(0, 100).map((job) => publicJob({
      id: `lever-${token}-${job.id}`,
      source: "lever",
      company: name,
      title: job.text || "",
      url: httpsUrl(job.hostedUrl || job.applyUrl),
      location_raw: (job.categories && job.categories.location) || "",
      posted_at: job.createdAt ? new Date(job.createdAt).toISOString() : "",
      updated_at: job.createdAt ? new Date(job.createdAt).toISOString() : "",
    })).filter((job) => job.id && job.url);
  }
  const res = await fetch(`https://boards-api.greenhouse.io/v1/boards/${token}/jobs`, {
    headers: { "user-agent": "JobAutopilot/1.0", accept: "application/json" },
  });
  if (!res.ok) return [];
  data = await res.json();
  return ((data && data.jobs) || []).slice(0, 100).map((job) => publicJob({
    id: `gh-${token}-${job.id}`,
    source: "greenhouse",
    company: name,
    title: job.title || "",
    url: httpsUrl(job.absolute_url),
    location_raw: (job.location && job.location.name) || "",
    posted_at: job.updated_at || "",
    updated_at: job.updated_at || "",
    department: (((job.departments || [])[0]) || {}).name || "",
  })).filter((job) => job.id && job.url);
}

export async function ingestNext(env, limit = 8) {
  if (!env || !env.DB) return { ingested: 0 };
  const offset = Number(await metaGet(env.DB, "ingest_offset") || 0);
  const slice = [];
  for (let i = 0; i < limit; i += 1) slice.push(catalog[(offset + i) % catalog.length]);
  const jobs = [];
  for (const company of slice) {
    try {
      jobs.push(...await fetchBoard(company));
    } catch (_) {
      /* one board failing does not stop the hour */
    }
  }
  const saved = await upsertJobs(env.DB, jobs);
  await metaSet(env.DB, "ingest_offset", String((offset + slice.length) % catalog.length));
  return { ingested: saved.changed.length, cursor: saved.cursor };
}

export async function sendSyncPing(env) {
  const payload = syncPushPayload();
  if (payload !== "{\"type\":\"sync\"}") return 0;
  if (!env || !env.VAPID_PUBLIC_KEY || !env.VAPID_PRIVATE_KEY || !env.DB) return 0;
  const result = await env.DB.prepare("SELECT subscription FROM push_subs").bind().all();
  const rows = result.results || [];
  let sent = 0;
  for (const row of rows) {
    try {
      const webpush = await import("web-push");
      const lib = webpush.default || webpush;
      lib.setVapidDetails("mailto:jobs@localhost", env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY);
      await lib.sendNotification(JSON.parse(row.subscription), payload);
      sent += 1;
    } catch (_) {
      /* a missing push library still leaves scoring on the device */
    }
  }
  return sent;
}
