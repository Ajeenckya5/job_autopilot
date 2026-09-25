import { rankAll } from "./logic/jobs.js";
import { loadLexicon } from "./logic/lexicon.js";

self.onmessage = async (event) => {
  const { jobs, profile } = event.data || {};
  await loadLexicon();
  self.postMessage(rankAll(jobs || [], profile || {}));
};
