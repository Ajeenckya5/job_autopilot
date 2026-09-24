export const EVENT_NAMES = ["onboarding_step", "search_run", "apply_clicked"];

export function eventBody(name) {
  if (!EVENT_NAMES.includes(name)) return "";
  return JSON.stringify({ name });
}

export function postEvent(api, name, config) {
  const base = String(api || "").replace(/\/$/, "");
  if (!base || (config && config.events === false)) return;
  const body = eventBody(name);
  if (!body) return;
  fetch(`${base}/v1/events`, {
    method: "POST",
    mode: "cors",
    headers: { "content-type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => {});
}
