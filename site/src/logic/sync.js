import { rankJob } from "./jobs.js";

export const SYNC_INTERVAL_MS = 10 * 60 * 1000;

export function newSinceLabel(count, since) {
  const time = new Date(since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${count} new since ${time}`;
}

export function apiBase() {
  let stored = "";
  try {
    stored = localStorage.getItem("jobAutopilotApi") || "";
  } catch (_) {
    stored = "";
  }
  const meta = typeof document !== "undefined"
    ? (document.querySelector('meta[name="jobs-api"]')?.content || "")
    : "";
  return String(stored || meta).replace(/\/$/, "");
}

export function applyDelta(existing, incoming, profile, now = Date.now()) {
  const map = new Map((existing || []).map((job) => [job.id, job]));
  const added = [];
  const wide = { ...(profile || {}), lookback_days: 30 };
  (incoming || []).forEach((job) => {
    const prev = map.get(job.id);
    if (prev) {
      const ranked = rankJob({
        ...job,
        status: prev.status,
        notes: prev.notes,
        applied_at: prev.applied_at,
      }, wide, now);
      if (ranked) {
        map.set(job.id, {
          ...ranked,
          status: prev.status || ranked.status || "new",
          notes: prev.notes || "",
          applied_at: prev.applied_at || "",
        });
      }
      return;
    }
    const ranked = rankJob(job, wide, now);
    if (!ranked || ranked.bucket !== "match") return;
    const row = { ...ranked, status: "new" };
    map.set(job.id, row);
    added.push(row);
  });
  return { jobs: [...map.values()], added };
}

export function startDeltaSync(run, env = globalThis) {
  const doc = env.document;
  let timer = 0;
  const hidden = () => !!(doc && doc.hidden);
  const tick = () => {
    if (!hidden()) run();
  };
  const arm = () => {
    clearInterval(timer);
    if (!hidden()) timer = setInterval(tick, SYNC_INTERVAL_MS);
  };
  if (doc && doc.addEventListener) doc.addEventListener("visibilitychange", arm);
  arm();
  tick();
  return () => {
    clearInterval(timer);
    if (doc && doc.removeEventListener) doc.removeEventListener("visibilitychange", arm);
  };
}

export async function pullDelta(api, since, country = "", family = "") {
  const base = String(api || "").replace(/\/$/, "");
  if (!base) return { jobs: [], cursor: since || "0", disabled: true };
  const url = new URL(`${base}/v1/jobs`);
  url.searchParams.set("since", since || "0");
  if (country) url.searchParams.set("country", country);
  if (family) url.searchParams.set("family", family);
  const response = await fetch(url, { mode: "cors" });
  if (!response.ok) throw new Error("delta failed");
  return response.json();
}

export async function pullConfig(api) {
  const base = String(api || "").replace(/\/$/, "");
  if (!base) return null;
  const response = await fetch(`${base}/v1/config`, { mode: "cors" });
  if (!response.ok) throw new Error("config failed");
  return response.json();
}
