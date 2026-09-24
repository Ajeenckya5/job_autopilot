import { openStore } from "../db.js";

export const STORAGE_BUDGET = 5 * 1024 * 1024;
const PRESSURE = 0.8 * STORAGE_BUDGET;

function localStorageBytes() {
  let bytes = 0;
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index) || "";
      bytes += (key.length + String(localStorage.getItem(key) || "").length) * 2;
    }
  } catch (_) {
    /* private mode can block localStorage */
  }
  return bytes;
}

export async function measureStorage() {
  let usage = 0;
  try {
    if (navigator.storage?.estimate) {
      const estimate = await navigator.storage.estimate();
      usage = Number(estimate.usage) || 0;
    }
  } catch (_) {
    usage = 0;
  }
  const local = localStorageBytes();
  if (usage < local) usage += local;
  return usage;
}

export function formatMegabytes(bytes) {
  return `${(Math.max(0, Number(bytes) || 0) / (1024 * 1024)).toFixed(1)} MB`;
}

async function evictOldestCaches() {
  if (typeof caches === "undefined" || !caches.keys) return;
  const names = (await caches.keys()).filter((name) => name.startsWith("job-autopilot-")).slice().sort();
  for (const name of names) {
    if (await measureStorage() < PRESSURE) return;
    await caches.delete(name);
  }
}

async function evictStoredCaches(db) {
  if (await measureStorage() >= PRESSURE && db.objectStoreNames.contains("shards")) {
    await db.clear("shards");
  }
  if (await measureStorage() < PRESSURE || !db.objectStoreNames.contains("llm-cache")) return;
  const rows = await db.getAll("llm-cache");
  rows.sort((a, b) => (a?.at || 0) - (b?.at || 0));
  for (const row of rows) {
    if (await measureStorage() < PRESSURE) return;
    if (row?.key) await db.delete("llm-cache", row.key);
  }
}

export async function enforceStorageBudget() {
  if (await measureStorage() < PRESSURE) return measureStorage();
  await evictOldestCaches();
  try {
    await evictStoredCaches(await openStore());
  } catch (_) {
    /* a blocked database still leaves the page usable */
  }
  return measureStorage();
}

export async function clearAppData() {
  try {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith("jobAutopilot")) localStorage.removeItem(key);
    }
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith("jobAutopilot")) sessionStorage.removeItem(key);
    }
  } catch (_) {
    /* ignore */
  }
  try {
    const db = await openStore();
    await Promise.all(["jobs", "kv", "shards", "feed", "llm-cache"].filter((name) => db.objectStoreNames.contains(name)).map((name) => db.clear(name)));
  } catch (_) {
    /* ignore */
  }
  try {
    if (typeof caches !== "undefined") {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name.startsWith("job-autopilot-")).map((name) => caches.delete(name)));
    }
  } catch (_) {
    /* ignore */
  }
}
