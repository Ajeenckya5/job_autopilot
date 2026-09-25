import { DatabaseSync } from "node:sqlite";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export function openLocalD1() {
  const db = new DatabaseSync(":memory:");
  const dir = join(dirname(fileURLToPath(import.meta.url)), "../migrations");
  readdirSync(dir).filter((name) => name.endsWith(".sql")).sort().forEach((name) => {
    db.exec(readFileSync(join(dir, name), "utf8"));
  });
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
    /** Like D1: every statement runs, in order, inside one transaction. */
    async batch(statements) {
      db.exec("BEGIN");
      try {
        const out = [];
        for (const statement of statements) out.push(await statement.run());
        db.exec("COMMIT");
        return out;
      } catch (err) {
        db.exec("ROLLBACK");
        throw err;
      }
    },
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
