import { createServer } from "node:http";
import { createApp } from "./src/index.js";
import { memoryKv, openLocalD1 } from "./src/sqlite-d1.js";

const port = Number(process.env.PORT || 8788);
const env = {
  DB: openLocalD1(),
  CONFIG: memoryKv(),
  INGEST_TOKEN: process.env.INGEST_TOKEN || "test-token",
};
const app = createApp();

createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks);
  const headers = new Headers();
  Object.entries(req.headers).forEach(([key, value]) => {
    if (value != null) headers.set(key, Array.isArray(value) ? value.join(", ") : value);
  });
  const response = await app.fetch(new Request(`http://127.0.0.1:${port}${req.url}`, {
    method: req.method,
    headers,
    body: body.length ? body : undefined,
  }), env);
  const bytes = Buffer.from(await response.arrayBuffer());
  const out = {};
  response.headers.forEach((value, key) => { out[key] = value; });
  res.writeHead(response.status, out);
  res.end(bytes);
}).listen(port, "127.0.0.1");
