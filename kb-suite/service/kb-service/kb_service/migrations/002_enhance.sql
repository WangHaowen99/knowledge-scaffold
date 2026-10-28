-- Question generation, LLM wiki, knowledge-base profile and per-document
-- processing overrides. Additive only: the v1 code keeps working on this
-- schema, so rolling the service back needs no down migration.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE documents
    ADD COLUMN question_status    text NOT NULL DEFAULT 'none',
    ADD COLUMN question_count     integer NOT NULL DEFAULT 0,
    ADD COLUMN wiki_status        text NOT NULL DEFAULT 'none',
    ADD COLUMN wiki_error         text,
    ADD COLUMN process_overrides  jsonb NOT NULL DEFAULT '{}';

ALTER TABLE upload_sessions
    ADD COLUMN process_overrides  jsonb NOT NULL DEFAULT '{}';

ALTER TABLE knowledge_bases
    ADD COLUMN profile             text,
    ADD COLUMN profile_status      text NOT NULL DEFAULT 'none',
    ADD COLUMN profile_digest      text,
    ADD COLUMN profile_updated_at  timestamptz;

-- Knowledge-base level jobs (wiki finalize, profile) carry kb_id so a burst
-- of document events collapses into one queued job.
ALTER TABLE jobs ADD COLUMN kb_id text;
CREATE INDEX jobs_kb_pending ON jobs (kind, kb_id) WHERE status = 'queued';

-- Generated questions are indexed as their own rows (WeKnora indexes each
-- question separately and resolves a hit to the parent chunk).
CREATE TABLE chunk_questions (
    id           text PRIMARY KEY,
    tenant_id    text NOT NULL,
    kb_id        text NOT NULL,
    document_id  text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id     text NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    seq          integer NOT NULL,
    question     text NOT NULL,
    embedding    halfvec,
    dimension    integer,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chunk_questions_chunk ON chunk_questions (chunk_id, seq);
CREATE INDEX chunk_questions_doc ON chunk_questions (document_id);
CREATE INDEX chunk_questions_bm25 ON chunk_questions
USING bm25 (id, kb_id, question)
WITH (
    key_field = 'id',
    text_fields = '{
        "question": {"tokenizer": {"type": "chinese_lindera"}}
    }'
);
CREATE INDEX chunk_questions_hnsw_1024 ON chunk_questions
USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE (dimension = 1024);

-- LLM wiki (page model follows WeKnora's wiki_pages: summary / entity /
-- concept / index pages linked with [[slug|text]]).
CREATE TABLE wiki_pages (
    id                text PRIMARY KEY,
    tenant_id         text NOT NULL,
    kb_id             text NOT NULL REFERENCES knowledge_bases(id),
    slug              text NOT NULL,
    title             text NOT NULL,
    title_norm        text NOT NULL,
    page_type         text NOT NULL,              -- summary | entity | concept | index
    summary           text NOT NULL DEFAULT '',
    content           text NOT NULL DEFAULT '',
    aliases           text[] NOT NULL DEFAULT '{}',
    source_doc_ids    text[] NOT NULL DEFAULT '{}',
    chunk_refs        jsonb NOT NULL DEFAULT '{}', -- {doc_id: [chunk_id, ...]}
    out_links         text[] NOT NULL DEFAULT '{}',
    in_links          text[] NOT NULL DEFAULT '{}',
    version           integer NOT NULL DEFAULT 1,
    last_edit_source  text NOT NULL DEFAULT 'pipeline',
    embedding         halfvec,
    dimension         integer,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz
);
CREATE UNIQUE INDEX wiki_pages_kb_slug ON wiki_pages (kb_id, slug) WHERE deleted_at IS NULL;
CREATE INDEX wiki_pages_kb_type ON wiki_pages (kb_id, page_type) WHERE deleted_at IS NULL;
CREATE INDEX wiki_pages_kb_title ON wiki_pages (kb_id, title_norm) WHERE deleted_at IS NULL;
CREATE INDEX wiki_pages_sources ON wiki_pages USING gin (source_doc_ids);
CREATE INDEX wiki_pages_title_trgm ON wiki_pages USING gin (title_norm gin_trgm_ops);
CREATE INDEX wiki_pages_bm25 ON wiki_pages
USING bm25 (id, kb_id, title, summary, content)
WITH (
    key_field = 'id',
    text_fields = '{
        "title": {"tokenizer": {"type": "chinese_lindera"}},
        "summary": {"tokenizer": {"type": "chinese_lindera"}},
        "content": {"tokenizer": {"type": "chinese_lindera"}}
    }'
);
CREATE INDEX wiki_pages_hnsw_1024 ON wiki_pages
USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE (dimension = 1024);

CREATE TABLE wiki_page_revisions (
    page_id      text NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    version      integer NOT NULL,
    title        text NOT NULL,
    summary      text NOT NULL,
    content      text NOT NULL,
    edit_source  text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, version)
);
