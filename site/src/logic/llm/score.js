import { buildProviderRequest, modelsRequest, parseModelList, readModelText } from "./providers.js";
import { buildPrompt, cacheKey, estimateTokens, parseScorePayload } from "./payload.js";

export class LlmError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function today(now) {
  return new Date(now).toISOString().slice(0, 10);
}

function nextUtcDay(now) {
  const date = new Date(now);
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + 1)).toISOString();
}

export function normalizeBudget(raw, now = Date.now()) {
  const day = today(now);
  const pausedUntil = raw?.pausedUntil || "";
  if (raw?.day !== day) return { day, count: 0, pausedUntil };
  return { day, count: Number(raw.count) || 0, pausedUntil };
}

export function budgetOpen(budget, now = Date.now()) {
  return !(budget?.pausedUntil && Date.parse(budget.pausedUntil) > now);
}

function publicHeaders(headers) {
  const out = {};
  Object.entries(headers || {}).forEach(([key, value]) => {
    if (/authorization|api-key|goog-api-key/i.test(key)) return;
    out[key] = value;
  });
  return out;
}

async function wait(ms, signal) {
  if (!ms) return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (!signal) return;
    const stop = () => {
      clearTimeout(timer);
      reject(new LlmError("timeout", "Stopped"));
    };
    if (signal.aborted) stop();
    else signal.addEventListener("abort", stop, { once: true });
  });
}

export async function postProvider(request, options) {
  const fetchImpl = options.fetchImpl || fetch;
  const mac = !!options.mac;
  const method = request.method || "POST";
  const init = mac
    ? {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        provider: options.provider,
        url: request.url,
        method,
        headers: publicHeaders(request.headers),
        body: request.body,
      }),
      signal: options.signal,
    }
    : {
      method,
      headers: request.headers,
      body: method === "GET" ? undefined : JSON.stringify(request.body),
      signal: options.signal,
    };
  return fetchImpl(mac ? "/api/llm/complete" : request.url, init);
}

function chunks(rows, size) {
  const out = [];
  for (let i = 0; i < rows.length; i += size) out.push(rows.slice(i, i + size));
  return out;
}

async function once(settings, prompt, options) {
  const request = buildProviderRequest(settings.provider, {
    model: settings.model,
    prompt,
    apiKey: options.mac ? "" : (settings.apiKey || ""),
  });
  let response;
  try {
    response = await postProvider(request, options);
  } catch (error) {
    if (error?.name === "TimeoutError" || error?.name === "AbortError") {
      throw new LlmError("timeout", "The provider took too long.");
    }
    throw new LlmError("down", "The provider is not reachable.");
  }
  if (response.status === 429) {
    const retryAfter = Number(response.headers?.get?.("retry-after")) || 2;
    await wait(Math.min(Math.max(retryAfter, 1), 30) * 1000, options.signal);
    response = await postProvider(request, options);
    if (response.status === 429) {
      throw new LlmError("quota", "The provider asked us to wait. AI scoring is paused until tomorrow.");
    }
  }
  if (response.status === 401 || response.status === 403) {
    throw new LlmError("key", "The provider rejected the key.");
  }
  if (!response.ok) throw new LlmError("down", "The provider returned an error. Local scores are unchanged.");
  return response.json();
}

async function scoreBatch(batch, profile, settings, options) {
  const prompt = buildPrompt(profile, batch, settings);
  let last = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const payload = await once(settings, prompt, options);
      const parsed = parseScorePayload(readModelText(settings.provider, payload), batch);
      if (parsed) return parsed;
      last = new LlmError("invalid", "The provider returned a score the app could not use.");
    } catch (error) {
      last = error;
      const retry = error instanceof LlmError && (error.code === "timeout" || error.code === "down" || error.code === "invalid");
      if (!retry) throw error;
    }
  }
  if (last && last.code !== "invalid") throw last;
  return null;
}

export async function listModels(settings, deps = {}) {
  if (!settings?.consent) throw new LlmError("consent", "Turn on consent before any AI request.");
  const request = modelsRequest(settings.provider, settings.mac ? "" : (settings.apiKey || ""));
  const response = await postProvider({ ...request, method: "GET" }, {
    mac: !!settings.mac,
    provider: settings.provider,
    fetchImpl: deps.fetchImpl || fetch,
    signal: deps.signal,
  });
  if (response.status === 401 || response.status === 403) throw new LlmError("key", "The provider rejected the key.");
  if (!response.ok) throw new LlmError("down", "The model list is not available.");
  return parseModelList(settings.provider, await response.json());
}

export async function scoreJobs(jobs, profile, settings, deps = {}) {
  if (!settings?.consent) {
    return { ok: false, code: "consent", message: "Turn on consent before any AI request.", results: new Map(), scored: 0 };
  }
  const now = deps.now || Date.now();
  const cap = Math.max(1, Math.min(1000, Number(settings.dailyCap) || 100));
  const topN = Math.max(1, Math.min(100, Number(settings.topN) || 30));
  const storage = deps.storage;
  let budget = normalizeBudget(storage ? await storage.get("llm-budget") : null, now);
  if (!budgetOpen(budget, now)) {
    return { ok: false, code: "quota", message: "AI scoring is paused until tomorrow.", results: new Map(), scored: 0 };
  }
  const ranked = [...(jobs || [])].sort((a, b) => (b.match_score || 0) - (a.match_score || 0)).slice(0, topN);
  const results = new Map();
  const pending = [];
  for (const job of ranked) {
    const key = cacheKey(job, profile, settings);
    const hit = storage ? await storage.getCache(key) : null;
    if (hit?.result) {
      results.set(job.id, hit.result);
    } else if (budget.count + pending.length < cap) {
      pending.push(job);
    }
  }
  const options = {
    mac: !!settings.mac,
    provider: settings.provider,
    fetchImpl: deps.fetchImpl || fetch,
    signal: deps.signal,
  };
  const size = Math.max(5, Math.min(10, Number(settings.batchSize) || 8));
  const groups = chunks(pending, size);
  const limit = Math.max(1, Number(settings.concurrency) || 1);
  let cursor = 0;
  let scored = 0;
  const progress = deps.onProgress || (() => {});
  progress({ total: ranked.length, done: results.size });
  const workers = Array.from({ length: Math.min(limit, groups.length || 1) }, async () => {
    while (cursor < groups.length) {
      const index = cursor;
      cursor += 1;
      const batch = groups[index];
      if (!batch) return;
      let parsed;
      try {
        parsed = await scoreBatch(batch, profile, settings, options);
      } catch (error) {
        if (error instanceof LlmError && error.code === "quota") {
          budget = { ...budget, pausedUntil: nextUtcDay(now) };
          if (storage) await storage.set("llm-budget", budget);
        }
        throw error;
      }
      budget = { ...budget, count: budget.count + batch.length, day: today(now) };
      if (storage) await storage.set("llm-budget", budget);
      scored += batch.length;
      if (parsed) {
        await Promise.all(batch.map(async (job, jobIndex) => {
          const result = parsed[jobIndex];
          results.set(job.id, result);
          if (storage) await storage.setCache({ key: cacheKey(job, profile, settings), result, at: now });
        }));
      }
      progress({ total: ranked.length, done: results.size });
    }
  });
  try {
    await Promise.all(workers);
  } catch (error) {
    return {
      ok: false,
      code: error.code || "down",
      message: error.message || "AI scoring stopped. Local scores are unchanged.",
      results,
      scored,
      tokens: estimateTokens(profile, pending, settings),
    };
  }
  return { ok: true, results, scored, tokens: estimateTokens(profile, pending, settings) };
}

export { estimateTokens };
