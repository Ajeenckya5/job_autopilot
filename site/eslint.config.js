import js from "@eslint/js";

export default [
  js.configs.recommended,
  {
    files: ["src/**/*.js", "test/**/*.js", "src/**/*.mjs", "test/**/*.mjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        document: "readonly",
        window: "readonly",
        localStorage: "readonly",
        caches: "readonly",
        sessionStorage: "readonly",
        location: "readonly",
        fetch: "readonly",
        Response: "readonly",
        crypto: "readonly",
        TextEncoder: "readonly",
        TextDecoder: "readonly",
        console: "readonly",
        Notification: "readonly",
        Worker: "readonly",
        confirm: "readonly",
        navigator: "readonly",
        URL: "readonly",
        AbortController: "readonly",
        Blob: "readonly",
        File: "readonly",
        TextEncoder: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        performance: "readonly",
        DataTransfer: "readonly",
        self: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" }],
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-control-regex": "off",
    },
  },
  {
    files: ["test/**/*.js"],
    languageOptions: {
      globals: {
        describe: "readonly",
        it: "readonly",
        expect: "readonly",
        Buffer: "readonly",
      },
    },
  },
];
