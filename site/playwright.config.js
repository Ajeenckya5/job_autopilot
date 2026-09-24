import { defineConfig, devices } from "@playwright/test";

const installedChrome = process.env.CI ? {} : { channel: "chrome" };

export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  outputDir: "test-results",
  use: {
    baseURL: "http://127.0.0.1:4174",
    video: "off",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "pnpm exec vite --host 127.0.0.1 --port 4174 --strictPort",
    port: 4174,
    reuseExistingServer: false,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], ...installedChrome } },
    ...(process.env.CI ? [{ name: "webkit", use: { ...devices["Desktop Safari"] } }] : []),
  ],
});
