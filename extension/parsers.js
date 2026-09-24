export function parseLinkedIn(html) {
  const text = String(html || "");
  const ids = [...text.matchAll(/\/jobs\/view\/(\d+)|currentJobId=(\d+)|urn:li:jobPosting:(\d+)/g)]
    .map((match) => match[1] || match[2] || match[3]);
  const titles = [...text.matchAll(/class="[^"]*base-search-card__title[^"]*"[^>]*>\s*([^<]+)/g)].map((match) => match[1].trim());
  const companies = [...text.matchAll(/class="[^"]*base-search-card__subtitle[^"]*"[^>]*>\s*([^<]+)/g)].map((match) => match[1].trim());
  const unique = [...new Set(ids)];
  return unique.map((id, index) => ({
    id: `li-${id}`,
    source: "linkedin",
    captured_via: "extension",
    title: titles[index] || "",
    company: companies[index] || "",
    url: `https://www.linkedin.com/jobs/view/${id}`,
    external_ids: { linkedin: id },
    confidence: titles[index] ? 0.9 : 0.7,
  }));
}

export function parseGoogleJobs(html) {
  const text = String(html || "");
  const blocks = [...text.matchAll(/<article[^>]*data-job="([^"]+)"[^>]*>/g)];
  return blocks.map((match) => {
    const raw = match[1];
    const [title, company, location] = raw.split("|");
    return {
      id: `gj-${title}-${company}`.toLowerCase().replace(/[^a-z0-9]+/g, "-"),
      source: "google_jobs",
      captured_via: "extension",
      title: title || "",
      company: company || "",
      location_raw: location || "",
      url: "",
      confidence: title && company ? 0.85 : 0.5,
    };
  });
}

export function parseJobright(html) {
  const text = String(html || "");
  const links = [...text.matchAll(/https:\/\/jobright\.ai\/jobs\/info\/([a-z0-9-]+)/g)];
  const titles = [...text.matchAll(/data-title="([^"]+)"/g)].map((match) => match[1]);
  const seen = new Set();
  return links.filter((match) => {
    if (seen.has(match[1])) return false;
    seen.add(match[1]);
    return true;
  }).map((match, index) => ({
    id: `jr-${match[1]}`,
    source: "jobright",
    captured_via: "extension",
    title: titles[index] || "",
    url: `https://jobright.ai/jobs/info/${match[1]}`,
    confidence: titles[index] ? 0.9 : 0.7,
  }));
}
