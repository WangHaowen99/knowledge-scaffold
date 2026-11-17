-- Point DA's "chinese" text search configuration at zhparser and recompute the stored vectors.
-- DA creates that configuration once, at schema creation, falling back to the default parser when
-- zhparser is missing; a database created that way keeps the fallback after zhparser is installed.
-- Idempotent: does nothing when zhparser is not available or the configuration already uses it.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'zhparser') THEN
    RAISE NOTICE 'zhparser is not installed in this Postgres image; Chinese keyword search keeps the default parser';
    RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_ts_config c JOIN pg_ts_parser p ON p.oid = c.cfgparser
             WHERE c.cfgname = 'chinese' AND p.prsname = 'zhparser') THEN
    RETURN;
  END IF;
  CREATE EXTENSION IF NOT EXISTS zhparser;
  DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese CASCADE;
  CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
  ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;
  -- the wiki_pages trigger recomputes fts_vector on an update of title or content
  UPDATE wiki_pages SET title = title;
  RAISE NOTICE 'chinese text search now uses zhparser';
END $$;
