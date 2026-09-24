const HOSTS = [
  { test: /greenhouse\.io$/, kind: "greenhouse" },
  { test: /lever\.co$/, kind: "lever" },
  { test: /ashbyhq\.com$/, kind: "ashby" },
];

function kindOf(host) {
  const hit = HOSTS.find((row) => row.test.test(host));
  return hit ? hit.kind : "";
}

function labelFor(field) {
  const id = field.getAttribute("id");
  const fromFor = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
  const wrap = field.closest("label");
  const aria = field.getAttribute("aria-label") || "";
  return `${fromFor ? fromFor.textContent : ""} ${wrap ? wrap.textContent : ""} ${aria} ${field.name || ""} ${field.placeholder || ""}`.toLowerCase();
}

function splitName(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  return { first: parts[0] || "", last: parts.slice(1).join(" ") };
}

function valueFor(label, profile) {
  const { first, last } = splitName(profile.name);
  if (/first/.test(label) && first) return first;
  if (/last/.test(label) && last) return last;
  if (/full name|^name|your name/.test(label) && profile.name) return profile.name;
  if (/email/.test(label) && profile.email) return profile.email;
  if (/phone|mobile/.test(label) && profile.phone) return profile.phone;
  if (/location|city/.test(label) && profile.location) return profile.location;
  if (/title|role|position/.test(label) && profile.role) return profile.role;
  return "";
}

export function fillForm(root, profile) {
  const fields = [...root.querySelectorAll("input, textarea")].filter((field) => {
    const type = (field.getAttribute("type") || "text").toLowerCase();
    return !["hidden", "submit", "button", "file", "checkbox", "radio", "password"].includes(type);
  });
  const filled = [];
  fields.forEach((field) => {
    const next = valueFor(labelFor(field), profile);
    if (!next || field.value) return;
    field.value = next;
    field.style.outline = "2px solid #0e6b45";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    filled.push(field);
  });
  return filled.length;
}

function paint(profile) {
  if (document.getElementById("job-autopilot-fill")) return;
  const button = document.createElement("button");
  button.id = "job-autopilot-fill";
  button.type = "button";
  button.textContent = "Fill from Job Autopilot";
  button.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483646;min-height:44px;padding:10px 14px;background:#f3f4ef;color:#101210;border:1px solid #f3f4ef;font:16px sans-serif;";
  button.addEventListener("click", () => {
    const count = fillForm(document, profile);
    button.textContent = count ? `Filled ${count}. Review before you submit.` : "Nothing on this form matched the profile.";
  });
  document.documentElement.appendChild(button);
}

const kind = kindOf(location.hostname);
if (kind && globalThis.chrome?.storage?.local) {
  chrome.storage.local.get("jobAutopilotFill", (stored) => {
    const profile = stored.jobAutopilotFill;
    if (!profile || !profile.name) return;
    paint(profile);
  });
}
