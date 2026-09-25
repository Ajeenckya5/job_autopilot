import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  // The learned vocabulary lives with the feed, one folder up.
  server: { fs: { allow: [".."] } },
  // The ranking worker loads the vocabulary on demand, which needs module workers.
  worker: { format: "es" },
  build: {
    outDir: "../docs",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "node",
    include: ["test/**/*.test.js"],
    setupFiles: ["test/setup.js"],
    exclude: ["e2e/**", "**/e2e/**", "node_modules/**"],
    coverage: {
      provider: "v8",
      include: ["src/logic/**/*.js"],
      thresholds: { lines: 80, functions: 80, statements: 80, branches: 60 },
    },
  },
});
