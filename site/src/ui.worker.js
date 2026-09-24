import { openStore, readSavedJobs } from "./db.js";
import { arrangeJobs } from "./logic/jobs.js";

let jobs = null;

self.onmessage = async (event) => {
  const started = performance.now();
  const data = event.data || {};
  try {
    if (data.jobs) jobs = data.jobs;
    if (!jobs || data.refresh) {
      const db = await openStore();
      jobs = await readSavedJobs(db);
    }
    if (data.jobPatch && data.jobPatch.id) {
      jobs = (jobs || []).map((job) => (job.id === data.jobPatch.id ? { ...job, ...data.jobPatch } : job));
    }
    const selected = arrangeJobs(jobs || [], data.profile || {}, data.controls || {});
    self.postMessage({
      generation: data.generation,
      rows: selected.rows.slice(0, 80),
      count: selected.rows.length,
      reasons: selected.reasons,
      ms: performance.now() - started,
    });
  } catch (error) {
    self.postMessage({
      generation: data.generation,
      error: error.message || "filter failed",
      rows: [],
      count: 0,
      reasons: [],
      ms: performance.now() - started,
    });
  }
};
