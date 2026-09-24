import { openDB } from "idb";

const DB_NAME = "job-autopilot";
const DB_VERSION = 4;
const LEGACY_JOBS = "jobAutopilotJobs";

export function openStore() {
  return openDB(DB_NAME, DB_VERSION, {
    upgrade(db) {
      if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
      if (!db.objectStoreNames.contains("jobs")) db.createObjectStore("jobs", { keyPath: "id" });
      if (!db.objectStoreNames.contains("shards")) db.createObjectStore("shards");
      if (!db.objectStoreNames.contains("feed")) db.createObjectStore("feed", { keyPath: "id" });
      if (!db.objectStoreNames.contains("llm-cache")) db.createObjectStore("llm-cache", { keyPath: "key" });
    },
  });
}

export async function readSavedJobs(db) {
  const rows = await db.getAll("jobs");
  return Array.isArray(rows) ? rows : [];
}

export async function writeSavedJobs(db, rows) {
  const tx = db.transaction("jobs", "readwrite");
  const writes = [tx.store.clear()];
  (rows || []).forEach((row, index) => {
    if (!row || typeof row !== "object") return;
    if (!row.status || row.status === "new") return;
    const copy = { ...row, id: row.id || `job-${index}` };
    delete copy.embedding;
    delete copy.vector;
    if (copy.description_text) copy.description_text = String(copy.description_text).slice(0, 400);
    writes.push(tx.store.put(copy));
  });
  await Promise.all([...writes, tx.done]);
}

export async function migrateLegacyJobs(db) {
  const existing = await readSavedJobs(db);
  let legacy = [];
  if (!existing.length) {
    try {
      legacy = JSON.parse(localStorage.getItem(LEGACY_JOBS) || "[]");
    } catch (_) {
      legacy = [];
    }
    if (!Array.isArray(legacy) || !legacy.length) {
      const kv = await db.get("kv", "jobs");
      legacy = Array.isArray(kv) ? kv : [];
    }
    if (legacy.length) await writeSavedJobs(db, legacy);
  }
  try {
    localStorage.removeItem(LEGACY_JOBS);
  } catch (_) {
    /* quota cleanup is best-effort */
  }
  try {
    await db.delete("kv", "jobs");
  } catch (_) {
    /* older databases may not have the key */
  }
  return existing.length ? existing : legacy;
}

export async function readShards(db, names) {
  const out = {};
  await Promise.all((names || []).map(async (name) => {
    const row = await db.get("shards", name);
    if (row) out[name] = row;
  }));
  return out;
}

export async function putShard(db, name, row) {
  await db.put("shards", row, name);
}

export async function upsertFeed(db, jobs) {
  const tx = db.transaction("feed", "readwrite");
  (jobs || []).forEach((job, index) => {
    if (!job) return;
    tx.store.put({ ...job, id: job.id || `feed-${index}` });
  });
  await tx.done;
}

export async function readFeed(db) {
  const rows = await db.getAll("feed");
  return Array.isArray(rows) ? rows : [];
}
