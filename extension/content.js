import { parseGoogleJobs, parseJobright, parseLinkedIn } from "./parsers.js";

function jobsOnPage() {
  const html = document.documentElement.innerHTML;
  const host = location.hostname;
  const path = location.pathname;
  if (host.includes("linkedin.com") || path.includes("/fixtures/linkedin")) return parseLinkedIn(html);
  if (host.includes("jobright.ai") || path.includes("/fixtures/jobright")) return parseJobright(html);
  if (host.includes("google.com") || path.includes("/fixtures/google")) return parseGoogleJobs(html);
  return [];
}

function paint(count) {
  let button = document.getElementById("job-autopilot-save");
  if (!button) {
    button = document.createElement("button");
    button.id = "job-autopilot-save";
    button.type = "button";
    button.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483646;min-height:44px;padding:10px 14px;background:#1b1914;color:#f7f1e6;border:0;font:16px sans-serif;";
    document.documentElement.appendChild(button);
    button.addEventListener("click", () => {
      const jobs = jobsOnPage();
      chrome.runtime.sendMessage({ type: "capture", jobs });
      button.textContent = `Saved ${jobs.length}`;
    });
  }
  button.textContent = count ? `Save ${count} jobs to Job Autopilot` : "No job cards on this page";
}

paint(jobsOnPage().length);
