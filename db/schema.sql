-- Permit Research Agent — Postgres schema.
--
-- Applied on startup via tools/db.py (CREATE ... IF NOT EXISTS everywhere),
-- not a migration framework. Keep this file idempotent.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fuzzy AHJ name matching (find_ahj)

CREATE TABLE IF NOT EXISTS ahj (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,          -- e.g. "city-of-austin-tx"
    name            TEXT NOT NULL,                 -- e.g. "City of Austin"
    jurisdiction_type TEXT NOT NULL CHECK (jurisdiction_type IN ('city', 'county', 'state', 'special_district')),
    state           TEXT NOT NULL,                 -- two-letter state code
    covers          TEXT[] DEFAULT '{}',            -- city/county names this AHJ covers, for address resolution
    website_url     TEXT,
    portal_url      TEXT,
    contact_info    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_verified_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ahj_name_trgm ON ahj USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_ahj_state ON ahj (state);

CREATE TABLE IF NOT EXISTS permit_type (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,   -- e.g. "Residential Electrical"
    category TEXT
);

CREATE TABLE IF NOT EXISTS permit_fees (
    id                 SERIAL PRIMARY KEY,
    ahj_id             INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    permit_type_id     INTEGER NOT NULL REFERENCES permit_type(id) ON DELETE CASCADE,
    fee_name           TEXT NOT NULL,
    amount             NUMERIC,
    unit               TEXT,           -- e.g. "flat", "per sq ft", "per $1000 valuation"
    calculation_basis  TEXT,
    source_url         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS permit_timelines (
    id                 SERIAL PRIMARY KEY,
    ahj_id             INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    permit_type_id     INTEGER NOT NULL REFERENCES permit_type(id) ON DELETE CASCADE,
    review_type        TEXT NOT NULL,  -- e.g. "standard", "expedited", "over-the-counter"
    duration_days      INTEGER,
    notes              TEXT,
    source_url         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS permit_deadlines (
    id                 SERIAL PRIMARY KEY,
    ahj_id             INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    permit_type_id     INTEGER NOT NULL REFERENCES permit_type(id) ON DELETE CASCADE,
    deadline_type       TEXT NOT NULL, -- e.g. "application_expiration", "permit_expiration", "renewal_window"
    description        TEXT NOT NULL,
    rule               TEXT,           -- e.g. "180 days from issuance if no inspection"
    source_url         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS permit_documents (
    id                 SERIAL PRIMARY KEY,
    ahj_id             INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    permit_type_id     INTEGER NOT NULL REFERENCES permit_type(id) ON DELETE CASCADE,
    document_name      TEXT NOT NULL,
    form_number        TEXT,
    is_required        BOOLEAN NOT NULL DEFAULT true,
    download_url       TEXT,
    source_url         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS submission_rules (
    id                 SERIAL PRIMARY KEY,
    ahj_id             INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    permit_type_id     INTEGER NOT NULL REFERENCES permit_type(id) ON DELETE CASCADE,
    methods            TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {"online_portal","mail","in_person"}
    steps              JSONB NOT NULL DEFAULT '[]',    -- ordered list of step descriptions
    portal_url         TEXT,
    source_url         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_documents (
    id                     SERIAL PRIMARY KEY,
    ahj_id                 INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    source_url             TEXT NOT NULL,
    storage_path           TEXT NOT NULL,   -- relative path under storage/raw/
    content_type           TEXT,            -- e.g. "text/html", "application/pdf"
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    extracted_text         TEXT,
    processed_structured   BOOLEAN NOT NULL DEFAULT false,
    processed_vector       BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_raw_documents_ahj ON raw_documents (ahj_id);
CREATE INDEX IF NOT EXISTS idx_raw_documents_unprocessed_structured ON raw_documents (ahj_id) WHERE NOT processed_structured;
CREATE INDEX IF NOT EXISTS idx_raw_documents_unprocessed_vector ON raw_documents (ahj_id) WHERE NOT processed_vector;

-- Embedding dimension must match tools/embeddings.py's model (default
-- BAAI/bge-small-en-v1.5 = 384). If EMBEDDING_MODEL changes to a different
-- dimension, this column must be recreated.
CREATE TABLE IF NOT EXISTS permit_chunks (
    id                 SERIAL PRIMARY KEY,
    ahj_id             INTEGER NOT NULL REFERENCES ahj(id) ON DELETE CASCADE,
    permit_type_id     INTEGER REFERENCES permit_type(id) ON DELETE CASCADE,
    raw_document_id    INTEGER NOT NULL REFERENCES raw_documents(id) ON DELETE CASCADE,
    section            TEXT,
    chunk_text         TEXT NOT NULL,
    embedding          vector(384) NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_permit_chunks_ahj ON permit_chunks (ahj_id);
-- IVFFlat needs rows to train lists on; harmless to create early, just less
-- effective until the table has meaningful data. Fine for this scale.
CREATE INDEX IF NOT EXISTS idx_permit_chunks_embedding ON permit_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS auth_users (
    id                 BIGSERIAL PRIMARY KEY,
    name               TEXT NOT NULL,
    email              TEXT NOT NULL UNIQUE,
    password_hash      TEXT,
    email_verified_at  TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_users_email_lower ON auth_users (lower(email));

CREATE TABLE IF NOT EXISTS auth_sessions (
    id          TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at ON auth_sessions (expires_at);

CREATE TABLE IF NOT EXISTS auth_oauth_states (
    id          TEXT PRIMARY KEY,
    next_path   TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_oauth_states_expires_at ON auth_oauth_states (expires_at);
