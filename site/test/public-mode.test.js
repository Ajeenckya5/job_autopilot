import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("public mode", () => {
  it("does not call scraping proxies", () => {
    const files = readdirSync(new URL("../src", import.meta.url)).filter((name) => name.endsWith(".js"));
    const logic = readdirSync(new URL("../src/logic", import.meta.url)).filter((name) => name.endsWith(".js"));
    const blobs = files.map((name) => readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8"));
    logic.forEach((name) => blobs.push(readFileSync(new URL(`../src/logic/${name}`, import.meta.url), "utf8")));
    const source = blobs.join("\n");
    expect(source).not.toMatch(/r\.jina\.ai|allorigins|linkedin\.com|indeed\.com|jobright\.ai/);
  });
});
