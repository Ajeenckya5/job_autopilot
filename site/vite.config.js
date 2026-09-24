import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  build: {
    outDir: "../docs",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "node",
    include: ["test/**/*.test.js"],
    exclude: ["e2e/**", "**/e2e/**", "node_modules/**"],
    coverage: {
      provider: "v8",
      include: ["src/logic/**/*.js"],
      thresholds: { lines: 80, functions: 80, statements: 80, branches: 60 },
    },
  },
});
