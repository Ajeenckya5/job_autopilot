const EMAIL = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
const TOKEN = /(bearer\s+)[a-z0-9._~+/-]+=*/gi;
const KEY = /\b(?:sk-|AIza|sk-or-|gsk_)[A-Za-z0-9_-]{8,}/g;
const DROP = new Set([
  "apikey", "api_key", "authorization", "resume_text", "resume", "prompt",
  "messages", "key", "token", "password", "payload", "x-api-key", "x-goog-api-key",
]);

export function stripEvent(event) {
  const clone = JSON.parse(JSON.stringify(event || {}));
  const walk = (value) => {
    if (typeof value === "string") {
      return value.replace(EMAIL, "[email]").replace(TOKEN, "$1[token]").replace(KEY, "[key]").replace(/\?[^"'\s]*/g, "");
    }
    if (Array.isArray(value)) return value.map(walk);
    if (value && typeof value === "object") {
      Object.keys(value).forEach((key) => {
        if (DROP.has(String(key).toLowerCase())) {
          value[key] = "[redacted]";
          return;
        }
        value[key] = walk(value[key]);
      });
    }
    return value;
  };
  return walk(clone);
}

export async function loadSentry(dsn) {
  if (!dsn) return;
  const Sentry = await import("@sentry/browser");
  Sentry.init({
    dsn,
    sendDefaultPii: false,
    beforeSend(event) {
      return stripEvent(event);
    },
  });
}
