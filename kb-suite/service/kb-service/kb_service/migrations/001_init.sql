-- kb-service initial schema. Vector and BM25 index layout follows WeKnora's
-- ParadeDB schema (migrations/paradedb/00-init-db.sql): halfvec with a
-- per-dimension partial HNSW index and a pg_search BM25 index using the
-- chinese_lindera tokenizer.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;

CREATE TABLE tenants (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE service_keys (
    id                 text PRIMARY KEY,
    tenant_id          text REFERENCES tenants(id),
    name               text NOT NULL,
    key_hash           text NOT NULL UNIQUE,
    capabilities       text[] NOT NULL,
    kb_allowlist       text[] NOT NULL DEFAULT '{}',
    can_assert_tenant  boolean NOT NULL DEFAULT false,
    disabled           boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_bases (
    id           text PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants(id),
    name         text NOT NULL,
    description  text NOT NULL DEFAULT '',
    config       jsonb NOT NULL DEFAULT '{}',
    embed_model  text NOT NULL,
    embed_dim    integer NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz
);
CREATE UNIQUE INDEX knowledge_bases_tenant_name
    ON knowledge_bases (tenant_id, name) WHERE deleted_at IS NULL;

CREATE TABLE documents (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants(id),
    kb_id           text NOT NULL REFERENCES knowledge_bases(id),
    source_type     text NOT NULL,              -- file | text | url
    file_name       text,
    file_type       text,
    file_size       bigint,
    storage_key     text,
    source_url      text,
    content_hash    text,
    folder_path     text NOT NULL DEFAULT '',
    title           text NOT NULL,
    parse_status    text NOT NULL DEFAULT 'pending',
    stage           text,
    attempt         integer NOT NULL DEFAULT 1,
    active_attempt  integer NOT NULL DEFAULT 0,
    error_code      text,
    error_message   text,
    summary         text,
    summary_status  text NOT NULL DEFAULT 'none',
    l1_key          text,
    l1_chars        integer,
    parse_meta      jsonb NOT NULL DEFAULT '{}',
    chunk_count     integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    deleted_at      timestamptz
);
CREATE INDEX documents_kb ON documents (kb_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX documents_kb_hash
    ON documents (kb_id, content_hash)
    WHERE deleted_at IS NULL AND content_hash IS NOT NULL;

CREATE TABLE chunks (
    id               text PRIMARY KEY,
    tenant_id        text NOT NULL,
    kb_id            text NOT NULL,
    document_id      text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    attempt          integer NOT NULL,
    seq              integer NOT NULL,
    chunk_type       text NOT NULL DEFAULT 'text',
    content          text NOT NULL,
    context_header   text NOT NULL DEFAULT '',
    start_offset     integer NOT NULL,
    end_offset       integer NOT NULL,
    line_start       integer NOT NULL,
    anchor_id        text,
    source_locators  jsonb NOT NULL DEFAULT '[]',
    embedding        halfvec,
    dimension        integer,
    is_active        boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chunks_doc_attempt ON chunks (document_id, attempt, seq);
CREATE INDEX chunks_kb_active ON chunks (kb_id) WHERE is_active;
CREATE INDEX chunks_bm25 ON chunks
USING bm25 (id, kb_id, document_id, content)
WITH (
    key_field = 'id',
    text_fields = '{
        "content": {"tokenizer": {"type": "chinese_lindera"}}
    }'
);
CREATE INDEX chunks_hnsw_1024 ON chunks
USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE (dimension = 1024);

CREATE TABLE document_assets (
    document_id  text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    name         text NOT NULL,
    storage_key  text NOT NULL,
    mime_type    text,
    PRIMARY KEY (document_id, name)
);

CREATE TABLE upload_sessions (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL,
    kb_id           text NOT NULL,
    file_name       text NOT NULL,
    file_size       bigint NOT NULL,
    sha256          text NOT NULL,
    folder_path     text NOT NULL DEFAULT '',
    part_size       integer NOT NULL,
    part_count      integer NOT NULL,
    parts_received  integer[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'open',
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL
);

-- Job queue lives in Postgres so a document row and its first job commit in
-- the same transaction (no "row without task" state to repair later).
CREATE TABLE jobs (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,
    document_id   text,
    payload       jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    attempts      integer NOT NULL DEFAULT 0,
    max_attempts  integer NOT NULL DEFAULT 3,
    run_after     timestamptz NOT NULL DEFAULT now(),
    locked_by     text,
    locked_at     timestamptz,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim ON jobs (run_after, id) WHERE status = 'queued';
CREATE INDEX jobs_running ON jobs (locked_at) WHERE status = 'running';
