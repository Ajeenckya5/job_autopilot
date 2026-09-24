const nonce = crypto.randomUUID();
let sent = false;

window.addEventListener("message", (event) => {
  if (sent || event.origin !== location.origin || !event.data) return;
  if (event.data.type !== "job-autopilot-ack" || event.data.nonce !== nonce) return;
  sent = true;
  chrome.runtime.sendMessage({ type: "flush" }, (response) => {
    const jobs = (response && response.jobs) || [];
    if (!jobs.length) return;
    window.postMessage({ type: "job-autopilot-jobs", nonce, jobs }, location.origin);
  });
});

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || !event.data || event.data.type !== "job-autopilot-profile") return;
  const profile = event.data.profile;
  if (!profile || !chrome.storage) return;
  chrome.storage.local.set({ jobAutopilotFill: profile });
});

function announce() {
  window.postMessage({ type: "job-autopilot-hello", nonce }, location.origin);
}

announce();
setTimeout(announce, 400);
