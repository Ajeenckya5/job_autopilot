export function jobsToCsv(jobs) {
  const headers = ["Status", "Score", "Title", "Company", "Location", "Posted", "URL", "Notes", "Why"];
  const lines = [headers.join(",")];
  (jobs || []).forEach((j) => {
    const row = [
      j.status || "new",
      j.match_score ?? "",
      j.title || "",
      j.company || "",
      j.location_raw || "",
      (j.posted_at || "").slice(0, 10),
      j.url || "",
      j.notes || "",
      (j.why_matched || []).join("; "),
    ].map((cell) => `"${String(cell).replace(/"/g, '""')}"`);
    lines.push(row.join(","));
  });
  return lines.join("\n");
}

function xml(s) {
  return String(s || "")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function crc32(u8) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < u8.length; i += 1) c = CRC[(c ^ u8[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}
const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
const u16 = (n) => Uint8Array.of(n & 255, (n >>> 8) & 255);
const u32 = (n) => Uint8Array.of(n & 255, (n >>> 8) & 255, (n >>> 16) & 255, (n >>> 24) & 255);
function concat(parts) {
  const len = parts.reduce((s, p) => s + p.length, 0);
  const out = new Uint8Array(len);
  let o = 0;
  parts.forEach((p) => { out.set(p, o); o += p.length; });
  return out;
}
function zipStore(files) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  files.forEach((f) => {
    const name = new TextEncoder().encode(f.name);
    const data = f.data;
    const crc = crc32(data);
    const local = concat([
      Uint8Array.of(0x50, 0x4b, 0x03, 0x04, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0),
      u32(crc), u32(data.length), u32(data.length), u16(name.length), u16(0),
      name, data,
    ]);
    const central = concat([
      Uint8Array.of(0x50, 0x4b, 0x01, 0x02, 20, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0),
      u32(crc), u32(data.length), u32(data.length), u16(name.length), u16(0),
      u16(0), u16(0), u16(0), u32(0), u32(offset), name,
    ]);
    locals.push(local);
    centrals.push(central);
    offset += local.length;
  });
  const center = concat(centrals);
  const eocd = concat([
    Uint8Array.of(0x50, 0x4b, 0x05, 0x06, 0, 0, 0, 0),
    u16(files.length), u16(files.length), u32(center.length), u32(offset), u16(0),
  ]);
  return new Blob([concat([...locals, center, eocd])], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}

export function jobsToXlsx(jobs) {
  const headers = ["Status", "Score", "Title", "Company", "Location", "Posted", "Apply", "Notes", "Why"];
  const rows = [headers].concat((jobs || []).map((j) => [
    j.status || "new",
    String(j.match_score ?? ""),
    j.title || "",
    j.company || "",
    j.location_raw || "",
    (j.posted_at || "").slice(0, 10),
    j.url && String(j.url).startsWith("https://") ? j.url : "",
    j.notes || "",
    (j.why_matched || []).join(", "),
  ]));
  const sheetRows = rows.map((row, r) => {
    const cells = row.map((val, c) => {
      const ref = `${String.fromCharCode(65 + c)}${r + 1}`;
      if (c === 6 && r > 0 && String(val).startsWith("https://")) {
        const safe = xml(val).replace(/"/g, "");
        return `<c r="${ref}"><f>HYPERLINK("${safe}","Apply")</f><v></v></c>`;
      }
      return `<c r="${ref}" t="inlineStr"><is><t>${xml(val).slice(0, 800)}</t></is></c>`;
    }).join("");
    return `<row r="${r + 1}">${cells}</row>`;
  }).join("");
  const sheet = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${sheetRows}</sheetData></worksheet>`;
  const workbook = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Jobs" sheetId="1" r:id="rId1"/></sheets></workbook>`;
  const rels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`;
  const wbRels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>`;
  const types = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>`;
  const enc = new TextEncoder();
  return zipStore([
    { name: "[Content_Types].xml", data: enc.encode(types) },
    { name: "_rels/.rels", data: enc.encode(rels) },
    { name: "xl/workbook.xml", data: enc.encode(workbook) },
    { name: "xl/_rels/workbook.xml.rels", data: enc.encode(wbRels) },
    { name: "xl/worksheets/sheet1.xml", data: enc.encode(sheet) },
  ]);
}
