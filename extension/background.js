const KEY = "jobAutopilotQueue";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "capture") return;
  chrome.storage.local.get(KEY, (stored) => {
    const queue = Array.isArray(stored[KEY]) ? stored[KEY] : [];
    const next = queue.concat(message.jobs || []).slice(-2000);
    chrome.storage.local.set({ [KEY]: next }, () => sendResponse({ ok: true, count: next.length }));
  });
  return true;
});

chrome.runtime.onMessageExternal.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "flush") return;
  chrome.storage.local.get(KEY, (stored) => {
    const jobs = Array.isArray(stored[KEY]) ? stored[KEY] : [];
    chrome.storage.local.set({ [KEY]: [] }, () => sendResponse({ jobs }));
  });
  return true;
});
