import { deflateSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import mammoth from "mammoth";
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import { jobsToXlsx } from "../src/logic/excel.js";
import { readResumeFile } from "../src/logic/resume.js";
import { stripEvent } from "../src/sentry.js";

function pdfBytes(text, compress) {
  const raw = Buffer.from(`BT /F1 18 Tf 72 720 Td (${text}) Tj ET`);
  const stream = compress ? deflateSync(raw) : raw;
  const filter = compress ? " /Filter /FlateDecode" : "";
  const header = Buffer.from("%PDF-1.4\n");
  const objects = [
    Buffer.from("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
    Buffer.from("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"),
    Buffer.from("3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"),
    Buffer.concat([
      Buffer.from(`4 0 obj\n<< /Length ${stream.length}${filter} >>\nstream\n`),
      stream,
      Buffer.from("\nendstream\nendobj\n"),
    ]),
    Buffer.from("5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"),
  ];
  let cursor = header.length;
  const offsets = [0];
  const parts = [header];
  objects.forEach((obj) => {
    offsets.push(cursor);
    parts.push(obj);
    cursor += obj.length;
  });
  let xref = "xref\n0 6\n0000000000 65535 f \n";
  for (let i = 1; i <= 5; i += 1) xref += `${String(offsets[i]).padStart(10, "0")} 00000 n \n`;
  parts.push(Buffer.from(`${xref}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${cursor}\n%%EOF\n`));
  return Buffer.concat(parts);
}

function fileFrom(name, bytes, type) {
  return {
    name,
    type,
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  };
}

describe("resume fixtures", () => {
  it("reads a compressed PDF", async () => {
    const bytes = pdfBytes("Ada Lovelace software engineer with Python and SQL experience", true);
    const got = await readResumeFile(fileFrom("cv.pdf", bytes, "application/pdf"), { pdfjs });
    expect(got.text.toLowerCase()).toContain("python");
    expect(got.skills).toContain("python");
  });
  it("reads an uncompressed PDF the same way", async () => {
    const bytes = pdfBytes("Ada Lovelace software engineer with Python and SQL experience", false);
    const got = await readResumeFile(fileFrom("docs.pdf", bytes, "application/pdf"), { pdfjs });
    expect(got.skills).toContain("sql");
  });
  it("rejects a scanned PDF with no text", async () => {
    const bytes = pdfBytes("", true);
    await expect(readResumeFile(fileFrom("scan.pdf", bytes, "application/pdf"), { pdfjs })).rejects.toThrow(/no text layer/);
  });
  it("reads a docx", async () => {
    const bytes = readFileSync(new URL("./fixtures/resume.docx", import.meta.url));
    const got = await readResumeFile(fileFrom("resume.docx", bytes, ""), { mammoth });
    expect(got.text.toLowerCase()).toContain("nurse");
  });
});

describe("excel", () => {
  it("writes https links as spreadsheet hyperlinks", () => {
    const blob = jobsToXlsx([{ title: "Engineer", url: "https://example.com/jobs/1", status: "new" }]);
    return blob.arrayBuffer().then((buf) => {
      const text = Buffer.from(buf).toString("latin1");
      expect(text).toContain("HYPERLINK");
      expect(text).toContain("https://example.com/jobs/1");
    });
  });
});

describe("monitoring", () => {
  it("strips emails, tokens, and query strings", () => {
    const clean = stripEvent({
      message: "failed for ada@example.com bearer abc123",
      request: { url: "https://example.com/path?token=secret" },
    });
    expect(JSON.stringify(clean)).not.toMatch(/ada@example.com/);
    expect(JSON.stringify(clean)).not.toMatch(/abc123/);
    expect(JSON.stringify(clean)).not.toMatch(/token=secret/);
  });
});
