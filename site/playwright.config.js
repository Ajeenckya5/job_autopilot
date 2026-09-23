import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  use: { baseURL: "http://127.0.0.1:4174" },
  webServer: {
    command: "npm run preview -- --host 127.0.0.1 --port 4174",
    port: 4174,
    reuseExistingServer: false,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], channel: process.env.CI ? undefined : "chrome" } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
});
