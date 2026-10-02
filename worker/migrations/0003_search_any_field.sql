-- Search by title and by skill phrases in any field, without fixed job families.
-- title_lc and text_lc hold the title and description folded for whole-word LIKE (searchable() in
-- the Worker). They start empty and are filled as the feed sends each job; the deploy that brings
-- this migration sends the whole feed. Rows are not backfilled here: D1 counts every row and index
-- entry written, and a backfill plus a full send would pass the free plan's daily write limit.
ALTER TABLE jobs ADD COLUMN title_lc TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN text_lc TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_jobs_country_posted ON jobs(country, posted_at);

-- Both served the five fixed families, which search no longer uses. Each costs a write per job.
DROP INDEX IF EXISTS idx_jobs_filter;
DROP INDEX IF EXISTS idx_jobs_search;
