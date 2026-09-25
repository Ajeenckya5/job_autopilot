-- Search by title and by skill phrases in any field, without fixed job families.
-- title_lc and text_lc hold the title and description folded for whole-word LIKE: lowercase, with
-- hyphens, slashes, underscores, apostrophes and common punctuation as spaces. New rows get the
-- same from searchable() in the Worker, which also drops accents and extra spaces; the next full
-- feed refreshes every row.
ALTER TABLE jobs ADD COLUMN title_lc TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN text_lc TEXT NOT NULL DEFAULT '';

UPDATE jobs SET
  title_lc = replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(lower(coalesce(json_extract(payload, '$.title'), '')), '-', ' '), '/', ' '), '_', ' '), '''', ' '), ',', ' '), '. ', ' '), ';', ' '), ':', ' '), '(', ' '), ')', ' '), '!', ' '), '?', ' '), '"', ' '), '|', ' '), char(10), ' '),
  text_lc = substr(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(lower(coalesce(json_extract(payload, '$.description_text'), '')), '-', ' '), '/', ' '), '_', ' '), '''', ' '), ',', ' '), '. ', ' '), ';', ' '), ':', ' '), '(', ' '), ')', ' '), '!', ' '), '?', ' '), '"', ' '), '|', ' '), char(10), ' '), 1, 4000);

CREATE INDEX IF NOT EXISTS idx_jobs_country_posted ON jobs(country, posted_at);
