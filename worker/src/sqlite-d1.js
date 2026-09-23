import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export function openLocalD1() {
  const db = new DatabaseSync(":memory:");
  const schema = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../schema.sql"), "utf8");
  db.exec(schema);
  return asD1(db);
}

export function memoryKv(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    async get(key) {
      return map.has(key) ? map.get(key) : null;
    },
    async put(key, value) {
      map.set(key, String(value));
    },
  };
}

export function asD1(db) {
  return {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async all() {
              const results = args.length ? db.prepare(sql).all(...args) : db.prepare(sql).all();
              return { results };
            },
            async first() {
              const row = args.length ? db.prepare(sql).get(...args) : db.prepare(sql).get();
              return row || null;
            },
            async run() {
              const info = args.length ? db.prepare(sql).run(...args) : db.prepare(sql).run();
              return { success: true, meta: { changes: info.changes } };
            },
          };
        },
      };
    },
  };
}
