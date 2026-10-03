-- V0005 — Qavanin sources & law-originals store
--
-- Operational store feeding the canonical graph:
--   sources.instruments       stable legal identity of each law (unifies records across sources)
--   sources.sources           registry of data-source portals (OpenCTI-style connector registry)
--   sources.source_documents  the law artifacts themselves (original PDF + extracted text per source)
--   sources.legal_effects     first-class legal change events (amend/repeal/suspend/...) with witness
--   sources.fetch_runs        checkpointed fetch runs per source (idempotent, resumable)
--   sources.extraction_jobs   LLM/parse extraction pipeline jobs per document (staged, reviewable)
--
-- Identity rules:
--   * an instrument is the stable legal identity; source documents reference it after matching
--   * a source document is idempotent on (source_id, content_checksum)
--   * a legal effect is idempotent on (affecting_document, type, target, witness_checksum)
--   * extraction outputs are idempotent on (document_uid, stage, input_checksum, prompt_version)
--   * review_status mirrors the canonical candidate -> expert_approved workflow
--   * authoritative temporal validity lives in the graph (effective ranges); legal_status here is
--     a denormalized cache for operations, never the source of truth

CREATE SCHEMA IF NOT EXISTS sources;

-- ---------------------------------------------------------------------------
-- stable legal identity of a law (Act) — one row per real-world law,
-- regardless of how many portals/files carry copies of it
-- ---------------------------------------------------------------------------
CREATE TABLE sources.instruments (
    instrument_uid   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_key    text NOT NULL UNIQUE CHECK (btrim(canonical_key) <> ''),
    canonical_title  text NOT NULL CHECK (btrim(canonical_title) <> ''),
    instrument_type  text NOT NULL DEFAULT 'unknown' CHECK (instrument_type IN (
                         'constitution', 'statute', 'special_statute', 'regulation',
                         'cabinet_approval', 'bylaw', 'circular', 'directive',
                         'judgment', 'bill', 'unknown')),
    tier             smallint CHECK (tier BETWEEN 1 AND 5),
    issuer           text,
    legal_status     text NOT NULL DEFAULT 'unknown' CHECK (legal_status IN (
                         'unknown', 'effective', 'amended', 'suspended', 'repealed', 'expired')),
    external_ids     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by       text NOT NULL DEFAULT 'system',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE sources.instruments IS
    'Stable legal identity (Act). Titles or document numbers are not identities: multiple source records attach here.';

-- ---------------------------------------------------------------------------
-- registry of data sources (dotic.ir, qavanin.ir, rc.majlis.ir, rrk.ir,
-- internal Baray 192.168.8.41:9000, user_upload, ...)
-- ---------------------------------------------------------------------------
CREATE TABLE sources.sources (
    source_id         text PRIMARY KEY,
    name              text NOT NULL CHECK (btrim(name) <> ''),
    kind              text NOT NULL CHECK (kind IN ('portal', 'api', 'baray_form', 'upload', 'rss')),
    base_url          text,
    enabled           boolean NOT NULL DEFAULT true,
    connector_version text NOT NULL DEFAULT '0',
    connector_config  jsonb NOT NULL DEFAULT '{}'::jsonb,
    checkpoint        jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- the law artifacts (اصل قوانین): original file + converted text, both kept.
-- one row per (source, artifact) — the same law on two portals = two rows,
-- unified through instrument_uid after matching.
-- ---------------------------------------------------------------------------
CREATE TABLE sources.source_documents (
    document_uid        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           text NOT NULL REFERENCES sources.sources (source_id) ON DELETE RESTRICT,
    external_id         text,
    -- linkage to the stable legal identity ----------------------------------
    instrument_uid      uuid REFERENCES sources.instruments (instrument_uid),
    match_status        text NOT NULL DEFAULT 'unmatched' CHECK (match_status IN (
                            'unmatched', 'auto', 'expert')),
    match_confidence    numeric(4, 3) CHECK (match_confidence IS NULL OR match_confidence BETWEEN 0 AND 1),
    -- classification -------------------------------------------------------
    doc_kind            text NOT NULL DEFAULT 'unknown' CHECK (doc_kind IN (
                            'constitution', 'statute', 'special_statute', 'regulation',
                            'cabinet_approval', 'bylaw', 'circular', 'directive',
                            'judgment', 'bill', 'unknown')),
    tier                smallint CHECK (tier BETWEEN 1 AND 5),
    instrument_no       text,
    title               text NOT NULL CHECK (btrim(title) <> ''),
    subtitle            text,
    origin_organization text,
    subject_group       text,
    scope               text,
    keywords            text[] NOT NULL DEFAULT '{}',
    language            text NOT NULL DEFAULT 'fa',
    -- legal dates, kept distinct (do NOT copy one into another) --------------
    issue_date          date,
    approval_date       date,
    notify_date         date,
    publication_date    date,
    gazette_no          text,
    gazette_date        date,
    gazette_page        text,
    effective_date      date,
    expiry_date         date,
    -- original artifact (PDF/scan) -----------------------------------------
    original_path       text CHECK (btrim(original_path) <> ''),
    original_media_type text,
    original_checksum   text CHECK (original_checksum IS NULL OR original_checksum ~ '^sha256:[0-9a-f]{64}$'),
    page_count          integer CHECK (page_count IS NULL OR page_count >= 0),
    -- converted text -------------------------------------------------------
    text_path           text,
    text_checksum       text CHECK (text_checksum IS NULL OR text_checksum ~ '^sha256:[0-9a-f]{64}$'),
    text_extract_method text CHECK (text_extract_method IN (
                            'pdftotext', 'vlm_ocr', 'html', 'markdown', 'manual', 'provided', NULL)),
    ocr_model           text,
    ocr_confidence      numeric(4, 3) CHECK (ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1),
    -- pipeline state -------------------------------------------------------
    fetch_state         text NOT NULL DEFAULT 'new' CHECK (fetch_state IN (
                            'new', 'fetched', 'text_extracted', 'parsed', 'extracted',
                            'reviewed', 'published', 'failed')),
    legal_status        text NOT NULL DEFAULT 'unknown' CHECK (legal_status IN (
                            'unknown', 'effective', 'amended', 'suspended', 'repealed', 'expired')),
    source_uri          text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_changed_at  timestamptz,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL DEFAULT 'system',
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, external_id)
);

COMMENT ON COLUMN sources.source_documents.legal_status IS
    'Denormalized cache for ops lists; authoritative status-at-date comes from graph effective ranges.';
COMMENT ON COLUMN sources.source_documents.effective_date IS
    'Not copied from issue_date: m2 civil code default (publication + 15 days) or the law own clause decides; pipeline rule.';

-- idempotency: same content from the same source is the same document
CREATE UNIQUE INDEX ix_source_documents_content
    ON sources.source_documents (source_id, original_checksum)
    WHERE original_checksum IS NOT NULL;

CREATE INDEX ix_source_documents_instrument ON sources.source_documents (instrument_uid);
CREATE INDEX ix_source_documents_kind ON sources.source_documents (doc_kind, tier);
CREATE INDEX ix_source_documents_fetch ON sources.source_documents (fetch_state);
CREATE INDEX ix_source_documents_gazette ON sources.source_documents (gazette_no, gazette_date);
CREATE INDEX ix_source_documents_title ON sources.source_documents
    USING gin (to_tsvector('simple', title));
CREATE INDEX ix_source_documents_keywords ON sources.source_documents USING gin (keywords);

-- ---------------------------------------------------------------------------
-- legal change events (رویداد تغییر حقوقی) — first-class, with witness.
-- the graph AMENDS/REPEALS/... edges are published FROM this table after review.
-- ---------------------------------------------------------------------------
CREATE TABLE sources.legal_effects (
    effect_uid              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    affecting_document_uid  uuid NOT NULL REFERENCES sources.source_documents (document_uid) ON DELETE CASCADE,
    affected_instrument_uid uuid REFERENCES sources.instruments (instrument_uid),
    affected_provision_label text,
    effect_type             text NOT NULL CHECK (effect_type IN (
                                'amend', 'append', 'supplement', 'repeal', 'replace',
                                'suspend', 'restore', 'annul')),
    mode                    text CHECK (mode IN ('explicit', 'implicit')),
    scope                   text CHECK (scope IN ('total', 'partial')),
    effective_at            date,
    witness_quote           text NOT NULL CHECK (btrim(witness_quote) <> ''),
    witness_page            integer,
    witness_checksum        text,
    confidence              numeric(4, 3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    detected_by             text NOT NULL DEFAULT 'text_rule' CHECK (detected_by IN (
                                'text_rule', 'llm', 'expert', 'import')),
    review_status           text NOT NULL DEFAULT 'candidate' CHECK (review_status IN (
                                'candidate', 'approved', 'rejected')),
    reviewed_by             text,
    reviewed_at             timestamptz,
    created_by              text NOT NULL DEFAULT 'system',
    created_at              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE sources.legal_effects IS
    'Legal change events: the affecting part, the affected part, the effective date and a checkable textual witness. A mere potential conflict is NOT a repeal (mode/review_status keep that honest).';

CREATE UNIQUE INDEX ix_legal_effects_idempotent
    ON sources.legal_effects (
        affecting_document_uid, effect_type,
        COALESCE(affected_instrument_uid::text, ''),
        COALESCE(affected_provision_label, ''),
        COALESCE(witness_checksum, ''))
    WHERE review_status <> 'rejected';

CREATE INDEX ix_legal_effects_review ON sources.legal_effects (review_status) WHERE review_status = 'candidate';
CREATE INDEX ix_legal_effects_affected ON sources.legal_effects (affected_instrument_uid);

-- ---------------------------------------------------------------------------
-- fetch runs (OpenCTI-style work with checkpoint) ---------------------------
-- ---------------------------------------------------------------------------
CREATE TABLE sources.fetch_runs (
    run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id         text NOT NULL REFERENCES sources.sources (source_id) ON DELETE CASCADE,
    connector_version text NOT NULL,
    status            text NOT NULL DEFAULT 'running' CHECK (status IN (
                          'running', 'succeeded', 'failed', 'cancelled')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    checkpoint        jsonb NOT NULL DEFAULT '{}'::jsonb,
    stats             jsonb NOT NULL DEFAULT '{}'::jsonb,
    error             text
);

CREATE INDEX ix_fetch_runs_source ON sources.fetch_runs (source_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- extraction pipeline jobs (parse / ocr / structure / enrich / ...) ----------
-- ---------------------------------------------------------------------------
CREATE TABLE sources.extraction_jobs (
    job_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_uid    uuid NOT NULL REFERENCES sources.source_documents (document_uid) ON DELETE CASCADE,
    stage           text NOT NULL CHECK (stage IN (
                        'text_extract', 'parse', 'structure', 'metadata_enrich',
                        'reference_resolution', 'amendment_detection', 'conflict_scan')),
    status          text NOT NULL DEFAULT 'queued' CHECK (status IN (
                        'queued', 'running', 'succeeded', 'failed')),
    model           text,
    prompt_version  text,
    input_checksum  text,
    output          jsonb,
    output_checksum text,
    review_status   text NOT NULL DEFAULT 'candidate' CHECK (review_status IN (
                        'candidate', 'approved', 'rejected')),
    reviewed_by     text,
    reviewed_at     timestamptz,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    UNIQUE (document_uid, stage, input_checksum, prompt_version)
);

CREATE INDEX ix_extraction_jobs_review ON sources.extraction_jobs (review_status) WHERE review_status = 'candidate';
CREATE INDEX ix_extraction_jobs_doc ON sources.extraction_jobs (document_uid, stage);

-- ---------------------------------------------------------------------------
-- trigger: keep updated_at honest
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sources.touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sources_touch
    BEFORE UPDATE ON sources.sources
    FOR EACH ROW EXECUTE FUNCTION sources.touch_updated_at();

CREATE TRIGGER trg_instruments_touch
    BEFORE UPDATE ON sources.instruments
    FOR EACH ROW EXECUTE FUNCTION sources.touch_updated_at();
