CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  country TEXT NOT NULL DEFAULT '',
  family TEXT NOT NULL DEFAULT '',
  company TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  cursor INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_cursor ON jobs(cursor);
CREATE INDEX IF NOT EXISTS idx_jobs_filter ON jobs(country, family, cursor);

CREATE TABLE IF NOT EXISTS company_weeks (
  company TEXT NOT NULL,
  week TEXT NOT NULL,
  new_jobs INTEGER NOT NULL,
  PRIMARY KEY (company, week)
);

CREATE TABLE IF NOT EXISTS funnel (
  week TEXT NOT NULL,
  name TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (week, name)
);

CREATE TABLE IF NOT EXISTS push_subs (
  endpoint_hash TEXT PRIMARY KEY,
  subscription TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
