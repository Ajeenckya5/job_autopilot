export function shardsToFetch(manifest, cached) {
  const hashes = (manifest && manifest.sha256) || {};
  const have = cached || {};
  return ((manifest && manifest.shards) || []).filter((name) => {
    const want = hashes[name];
    return !want || have[name] !== want;
  });
}

export function dropStale(job) {
  const company = String((job && job.company) || "").replace(/\s+/g, " ").trim().toLowerCase();
  const id = String((job && job.id) || "");
  if (id.startsWith("ashby-anthropic")) return true;
  return job && job.source === "ashby" && company === "anthropic";
}
