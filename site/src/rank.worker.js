import { rankAll } from "./logic/jobs.js";

self.onmessage = (event) => {
  const { jobs, profile } = event.data || {};
  self.postMessage(rankAll(jobs || [], profile || {}));
};
