CREATE SCHEMA IF NOT EXISTS ingestion;

CREATE TABLE ingestion.jobs (
    job_id text PRIMARY KEY,
    organization_id text,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    source_uri text NOT NULL CHECK (btrim(source_uri) <> ''),
    parser_name text NOT NULL CHECK (btrim(parser_name) <> ''),
    parser_version text NOT NULL CHECK (btrim(parser_version) <> ''),
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead_letter')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    locked_by text,
    last_error text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (attempts <= max_attempts),
    CHECK (
        (status = 'running' AND locked_at IS NOT NULL AND locked_by IS NOT NULL)
        OR (status <> 'running' AND locked_at IS NULL AND locked_by IS NULL)
    ),
    CHECK (status <> 'succeeded' OR result IS NOT NULL)
);

CREATE UNIQUE INDEX uq_ingestion_job_idempotency
    ON ingestion.jobs (COALESCE(organization_id, '__shared__'), idempotency_key);
CREATE INDEX ix_ingestion_jobs_claim
    ON ingestion.jobs (available_at, created_at, job_id)
    WHERE status IN ('queued', 'failed');

CREATE TABLE ingestion.correction_annotations (
    annotation_id text PRIMARY KEY,
    organization_id text,
    source_document_id text NOT NULL REFERENCES canonical.source_documents(source_document_id),
    source_span_id text NOT NULL REFERENCES canonical.source_spans(source_span_id),
    original_text text NOT NULL CHECK (btrim(original_text) <> ''),
    corrected_text text NOT NULL CHECK (btrim(corrected_text) <> ''),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    status text NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by text,
    reviewed_at timestamptz,
    review_note text,
    CHECK (original_text <> corrected_text),
    CHECK (
        (status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL)
        OR (status IN ('approved', 'rejected') AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    )
);

CREATE INDEX ix_correction_annotations_source
    ON ingestion.correction_annotations (source_document_id, source_span_id, created_at);

CREATE TABLE ingestion.amendment_links (
    amendment_link_id text PRIMARY KEY,
    amending_document_version_id text NOT NULL
        REFERENCES canonical.document_versions(document_version_id),
    amended_document_version_id text NOT NULL
        REFERENCES canonical.document_versions(document_version_id),
    source_span_id text REFERENCES canonical.source_spans(source_span_id),
    relation_type text NOT NULL CHECK (
        relation_type IN ('amends', 'repeals', 'replaces', 'suspends', 'restores')
    ),
    effective_from date,
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (amending_document_version_id <> amended_document_version_id),
    UNIQUE (amending_document_version_id, amended_document_version_id, relation_type, effective_from)
);

CREATE INDEX ix_amendment_links_amended
    ON ingestion.amendment_links (amended_document_version_id, effective_from);

COMMENT ON TABLE ingestion.jobs IS
    'Idempotent parser/OCR work queue. Workers claim rows with FOR UPDATE SKIP LOCKED.';
COMMENT ON TABLE ingestion.correction_annotations IS
    'Reviewable corrections; immutable canonical source text is never overwritten.';
COMMENT ON TABLE ingestion.amendment_links IS
    'Auditable document-version amendment relations with optional exact source span.';
