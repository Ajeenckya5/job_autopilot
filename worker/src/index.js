import { Hono } from "hono";
import { cors } from "hono/cors";
import {
  DEFAULT_CONFIG,
  companyTrend,
  eventName,
  funnelRows,
  ingestNext,
  jobById,
  jobsSince,
  profileVectorInput,
  embedProfile,
  recordEvent,
  searchJobs,
  sha256,
  upsertJobs,
} from "./jobs.js";

export function createApp() {
  const app = new Hono();
  app.use("*", cors({ origin: "*" }));

  app.get("/v1/config", async (c) => {
    let stored = null;
    if (c.env.CONFIG) stored = await c.env.CONFIG.get("config");
    let extra = {};
    if (stored) {
      try { extra = JSON.parse(stored); } catch (_) { extra = {}; }
    }
    const config = { ...DEFAULT_CONFIG, ...extra };
    if (c.env.VAPID_PUBLIC_KEY) config.vapid_public_key = c.env.VAPID_PUBLIC_KEY;
    delete config.VAPID_PRIVATE_KEY;
    return c.json(config);
  });

  app.get("/v1/search", async (c) => {
    const config = await readConfig(c.env);
    if (config.api === false) return c.json({ jobs: [], disabled: true });
    const url = new URL(c.req.url);
    const page = await searchJobs(c.env.DB, {
      countries: url.searchParams.getAll("country"),
      families: url.searchParams.getAll("family"),
      since: url.searchParams.get("since") || "",
      limit: url.searchParams.get("limit") || "300",
      terms: url.searchParams.getAll("term"),
    });
    return c.json(page);
  });

  app.post("/v1/profile-vector", async (c) => {
    const raw = await c.req.text();
    if (raw.length > 8000) return c.json({ ok: false }, 413);
    let parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch (_) {
      return c.json({ ok: false }, 400);
    }
    const body = profileVectorInput(parsed);
    if (body.error) return c.json({ ok: false }, 400);
    return c.json({ vector: embedProfile(body) });
  });

  app.get("/v1/job/:id", async (c) => {
    const config = await readConfig(c.env);
    if (config.api === false) return c.json({ description_text: "" }, 404);
    const job = await jobById(c.env.DB, c.req.param("id"));
    if (!job) return c.json({ description_text: "" }, 404);
    return c.json(job);
  });

  app.get("/v1/jobs", async (c) => {
    const config = await readConfig(c.env);
    if (config.api === false || config.sync === false) return c.json({ jobs: [], cursor: c.req.query("since") || "0", disabled: true });
    const since = c.req.query("since") || "0";
    const country = String(c.req.query("country") || "").slice(0, 40);
    const family = String(c.req.query("family") || "").slice(0, 40);
    const page = await jobsSince(c.env.DB, since, country, family);
    return c.json(page);
  });

  app.post("/v1/jobs", async (c) => {
    const expected = c.env.INGEST_TOKEN || "";
    if (!expected) return c.json({ ok: false }, 404);
    if (c.req.header("x-ingest-token") !== expected) return c.json({ ok: false }, 401);
    const body = await c.req.json().catch(() => null);
    const saved = await upsertJobs(c.env.DB, (body && body.jobs) || []);
    return c.json({ ok: true, changed: saved.changed.length, cursor: saved.cursor });
  });

  app.post("/v1/events", async (c) => {
    const config = await readConfig(c.env);
    if (config.events === false) return c.json({ ok: false }, 404);
    const raw = await c.req.text();
    const parsed = eventName(raw);
    if (parsed.error) return c.json({ ok: false }, 400);
    const week = await recordEvent(c.env.DB, parsed.name);
    return c.json({ ok: true, week });
  });

  app.get("/v1/funnel", async (c) => {
    const rows = await funnelRows(c.env.DB);
    return c.json({ weeks: rows });
  });

  app.get("/v1/companies/:company/trend", async (c) => {
    const company = decodeURIComponent(c.req.param("company") || "").replace(/\s+/g, " ").trim().slice(0, 120);
    if (!company) return c.json({ weeks: [] }, 400);
    const weeks = await companyTrend(c.env.DB, company);
    return c.json({ company, weeks });
  });

  app.post("/v1/push/subscribe", async (c) => {
    const config = await readConfig(c.env);
    if (config.push === false) return c.json({ ok: false }, 404);
    const body = await c.req.json().catch(() => null);
    const subscription = body && body.subscription;
    if (!subscription || !subscription.endpoint || String(subscription.endpoint).length > 2000) {
      return c.json({ ok: false }, 400);
    }
    const endpointHash = await sha256(subscription.endpoint);
    const stored = JSON.stringify({ endpoint: subscription.endpoint, keys: subscription.keys || {} });
    await c.env.DB.prepare(
      "INSERT INTO push_subs (endpoint_hash, subscription) VALUES (?, ?) ON CONFLICT(endpoint_hash) DO UPDATE SET subscription = excluded.subscription",
    ).bind(endpointHash, stored).run();
    return c.json({ ok: true });
  });

  app.get("/health", (c) => c.json({ ok: true }));
  return app;
}

async function readConfig(env) {
  if (!env || !env.CONFIG) return DEFAULT_CONFIG;
  const stored = await env.CONFIG.get("config");
  if (!stored) return DEFAULT_CONFIG;
  try {
    return { ...DEFAULT_CONFIG, ...JSON.parse(stored) };
  } catch (_) {
    return DEFAULT_CONFIG;
  }
}

const app = createApp();

export default {
  fetch(request, env, ctx) {
    return app.fetch(request, env, ctx);
  },
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(ingestNext(env, 8));
  },
};
