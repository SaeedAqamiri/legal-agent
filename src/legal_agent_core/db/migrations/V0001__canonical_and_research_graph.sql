CREATE SCHEMA IF NOT EXISTS canonical;
CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE canonical.source_documents (
    source_document_id text PRIMARY KEY,
    organization_id text,
    filename text NOT NULL CHECK (btrim(filename) <> ''),
    media_type text NOT NULL CHECK (btrim(media_type) <> ''),
    checksum text NOT NULL CHECK (btrim(checksum) <> ''),
    file_size bigint NOT NULL CHECK (file_size >= 0),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    ingested_by text NOT NULL CHECK (btrim(ingested_by) <> ''),
    source_uri text NOT NULL CHECK (btrim(source_uri) <> ''),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0)
);

CREATE UNIQUE INDEX uq_source_document_checksum_scope
    ON canonical.source_documents (COALESCE(organization_id, '__shared__'), checksum);

CREATE TABLE canonical.legal_instruments (
    instrument_id text PRIMARY KEY,
    title text NOT NULL CHECK (btrim(title) <> ''),
    canonical_title text NOT NULL CHECK (btrim(canonical_title) <> ''),
    instrument_type text NOT NULL CHECK (btrim(instrument_type) <> ''),
    jurisdiction text NOT NULL CHECK (btrim(jurisdiction) <> ''),
    issuer text,
    authority_level text,
    subject_domain text,
    language text NOT NULL DEFAULT 'fa',
    external_identifier text,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0)
);

CREATE UNIQUE INDEX uq_legal_instrument_external_identity
    ON canonical.legal_instruments (jurisdiction, external_identifier)
    WHERE external_identifier IS NOT NULL;

CREATE TABLE canonical.document_versions (
    document_version_id text PRIMARY KEY,
    instrument_id text NOT NULL REFERENCES canonical.legal_instruments(instrument_id),
    source_document_id text NOT NULL REFERENCES canonical.source_documents(source_document_id),
    version_label text,
    version_number integer CHECK (version_number IS NULL OR version_number > 0),
    publication_date date,
    enacted_at date,
    effective_from date,
    effective_to date,
    repealed_at date,
    status text NOT NULL CHECK (
        status IN ('draft', 'published', 'effective', 'suspended', 'expired', 'repealed', 'superseded', 'unknown')
    ),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
    UNIQUE (document_version_id, instrument_id),
    UNIQUE (document_version_id, source_document_id)
);

CREATE INDEX ix_document_versions_temporal
    ON canonical.document_versions (instrument_id, effective_from, effective_to);

CREATE TABLE canonical.provisions (
    provision_id text PRIMARY KEY,
    instrument_id text NOT NULL REFERENCES canonical.legal_instruments(instrument_id),
    provision_type text NOT NULL CHECK (btrim(provision_type) <> ''),
    number text,
    label text NOT NULL CHECK (btrim(label) <> ''),
    title text,
    parent_provision_id text,
    ordinal integer NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
    depth integer NOT NULL DEFAULT 0 CHECK (depth >= 0),
    human_key text,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    CHECK (parent_provision_id IS NULL OR parent_provision_id <> provision_id),
    UNIQUE (provision_id, instrument_id),
    UNIQUE (instrument_id, parent_provision_id, ordinal),
    FOREIGN KEY (parent_provision_id, instrument_id)
        REFERENCES canonical.provisions(provision_id, instrument_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX ix_provisions_parent ON canonical.provisions (instrument_id, parent_provision_id, ordinal);

CREATE TABLE canonical.provision_versions (
    provision_version_id text PRIMARY KEY,
    provision_id text NOT NULL,
    document_version_id text NOT NULL,
    instrument_id text NOT NULL,
    text text NOT NULL CHECK (btrim(text) <> ''),
    normalized_text text NOT NULL CHECK (btrim(normalized_text) <> ''),
    effective_from date,
    effective_to date,
    status text NOT NULL CHECK (
        status IN ('draft', 'published', 'effective', 'suspended', 'expired', 'repealed', 'superseded', 'unknown')
    ),
    created_from text NOT NULL CHECK (btrim(created_from) <> ''),
    supersedes_version_id text REFERENCES canonical.provision_versions(provision_version_id),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
    CHECK (supersedes_version_id IS NULL OR supersedes_version_id <> provision_version_id),
    FOREIGN KEY (provision_id, instrument_id)
        REFERENCES canonical.provisions(provision_id, instrument_id),
    FOREIGN KEY (document_version_id, instrument_id)
        REFERENCES canonical.document_versions(document_version_id, instrument_id),
    UNIQUE (provision_version_id, provision_id),
    UNIQUE (provision_version_id, document_version_id),
    UNIQUE (provision_version_id, provision_id, document_version_id),
    FOREIGN KEY (supersedes_version_id, provision_id)
        REFERENCES canonical.provision_versions(provision_version_id, provision_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX ix_provision_versions_temporal
    ON canonical.provision_versions (provision_id, effective_from, effective_to);
CREATE INDEX ix_provision_versions_supersedes
    ON canonical.provision_versions (supersedes_version_id)
    WHERE supersedes_version_id IS NOT NULL;

CREATE TABLE canonical.source_spans (
    source_span_id text PRIMARY KEY,
    source_document_id text NOT NULL REFERENCES canonical.source_documents(source_document_id),
    document_version_id text NOT NULL REFERENCES canonical.document_versions(document_version_id),
    provision_version_id text NOT NULL REFERENCES canonical.provision_versions(provision_version_id),
    page_number integer NOT NULL CHECK (page_number >= 1),
    bbox_x1 double precision,
    bbox_y1 double precision,
    bbox_x2 double precision,
    bbox_y2 double precision,
    char_start integer,
    char_end integer,
    raw_text text NOT NULL CHECK (btrim(raw_text) <> ''),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    CHECK (
        (bbox_x1 IS NULL AND bbox_y1 IS NULL AND bbox_x2 IS NULL AND bbox_y2 IS NULL)
        OR
        (0 <= bbox_x1 AND bbox_x1 < bbox_x2 AND bbox_x2 <= 1
         AND 0 <= bbox_y1 AND bbox_y1 < bbox_y2 AND bbox_y2 <= 1)
    ),
    CHECK (
        (char_start IS NULL AND char_end IS NULL)
        OR (char_start >= 0 AND char_end >= char_start)
    ),
    UNIQUE (source_span_id, provision_version_id),
    FOREIGN KEY (document_version_id, source_document_id)
        REFERENCES canonical.document_versions(document_version_id, source_document_id),
    FOREIGN KEY (provision_version_id, document_version_id)
        REFERENCES canonical.provision_versions(provision_version_id, document_version_id)
);

CREATE INDEX ix_source_spans_citation
    ON canonical.source_spans (provision_version_id, page_number);

CREATE TABLE canonical.explicit_references (
    reference_id text PRIMARY KEY,
    source_provision_version_id text NOT NULL REFERENCES canonical.provision_versions(provision_version_id),
    source_span_id text NOT NULL REFERENCES canonical.source_spans(source_span_id),
    target_text text NOT NULL CHECK (btrim(target_text) <> ''),
    resolved_target_provision_id text REFERENCES canonical.provisions(provision_id),
    resolution_status text NOT NULL CHECK (resolution_status IN ('unresolved', 'resolved', 'ambiguous', 'invalid')),
    confidence double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    extraction_method text NOT NULL CHECK (btrim(extraction_method) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_span_id, source_provision_version_id)
        REFERENCES canonical.source_spans(source_span_id, provision_version_id),
    CHECK (
        (resolution_status = 'resolved' AND resolved_target_provision_id IS NOT NULL)
        OR (resolution_status <> 'resolved' AND resolved_target_provision_id IS NULL)
    )
);

CREATE TABLE canonical.citations (
    citation_id text PRIMARY KEY,
    instrument_id text NOT NULL REFERENCES canonical.legal_instruments(instrument_id),
    document_version_id text NOT NULL REFERENCES canonical.document_versions(document_version_id),
    provision_id text NOT NULL REFERENCES canonical.provisions(provision_id),
    provision_version_id text NOT NULL REFERENCES canonical.provision_versions(provision_version_id),
    source_span_id text NOT NULL REFERENCES canonical.source_spans(source_span_id),
    page integer NOT NULL CHECK (page >= 1),
    quoted_text text NOT NULL CHECK (btrim(quoted_text) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (provision_version_id, provision_id, document_version_id)
        REFERENCES canonical.provision_versions(provision_version_id, provision_id, document_version_id),
    FOREIGN KEY (provision_id, instrument_id)
        REFERENCES canonical.provisions(provision_id, instrument_id),
    FOREIGN KEY (document_version_id, instrument_id)
        REFERENCES canonical.document_versions(document_version_id, instrument_id),
    FOREIGN KEY (source_span_id, provision_version_id)
        REFERENCES canonical.source_spans(source_span_id, provision_version_id)
);

CREATE TABLE canonical.graph_edges (
    edge_id text PRIMARY KEY,
    source_node_type text NOT NULL CHECK (btrim(source_node_type) <> ''),
    source_node_id text NOT NULL CHECK (btrim(source_node_id) <> ''),
    target_node_type text NOT NULL CHECK (btrim(target_node_type) <> ''),
    target_node_id text NOT NULL CHECK (btrim(target_node_id) <> ''),
    edge_type text NOT NULL CHECK (btrim(edge_type) <> ''),
    source_span_id text REFERENCES canonical.source_spans(source_span_id),
    extraction_method text NOT NULL CHECK (btrim(extraction_method) <> ''),
    confidence double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    source_id text NOT NULL CHECK (btrim(source_id) <> ''),
    parser_version text,
    model_id text,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    CHECK (source_node_id <> target_node_id),
    CHECK (edge_type <> 'explicitly_references' OR source_span_id IS NOT NULL)
);

CREATE INDEX ix_canonical_edges_outgoing
    ON canonical.graph_edges (source_node_type, source_node_id, edge_type);
CREATE INDEX ix_canonical_edges_incoming
    ON canonical.graph_edges (target_node_type, target_node_id, edge_type);

CREATE OR REPLACE FUNCTION canonical.block_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'canonical records are append-only; archive or add a new version instead';
END;
$$;

CREATE OR REPLACE FUNCTION canonical.protect_provision_text()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.text IS DISTINCT FROM OLD.text OR NEW.normalized_text IS DISTINCT FROM OLD.normalized_text THEN
        RAISE EXCEPTION 'provision text is immutable; create a new provision version';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION canonical.protect_source_span_text()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.raw_text IS DISTINCT FROM OLD.raw_text THEN
        RAISE EXCEPTION 'raw source text is immutable; create a correction/version annotation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION canonical.validate_provision_hierarchy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_depth integer;
BEGIN
    IF NEW.parent_provision_id IS NULL THEN
        IF NEW.depth <> 0 THEN
            RAISE EXCEPTION 'root provision depth must be zero';
        END IF;
        RETURN NEW;
    END IF;
    SELECT depth INTO parent_depth
    FROM canonical.provisions
    WHERE provision_id = NEW.parent_provision_id AND instrument_id = NEW.instrument_id;
    IF parent_depth IS NULL OR NEW.depth <> parent_depth + 1 THEN
        RAISE EXCEPTION 'child provision depth must equal parent depth plus one';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER protect_provision_text
    BEFORE UPDATE ON canonical.provision_versions
    FOR EACH ROW EXECUTE FUNCTION canonical.protect_provision_text();
CREATE TRIGGER protect_source_span_text
    BEFORE UPDATE ON canonical.source_spans
    FOR EACH ROW EXECUTE FUNCTION canonical.protect_source_span_text();
CREATE TRIGGER validate_provision_hierarchy
    BEFORE INSERT OR UPDATE OF parent_provision_id, depth ON canonical.provisions
    FOR EACH ROW EXECUTE FUNCTION canonical.validate_provision_hierarchy();

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'source_documents', 'legal_instruments', 'document_versions', 'provisions',
        'provision_versions', 'source_spans', 'explicit_references', 'citations', 'graph_edges'
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER block_canonical_delete BEFORE DELETE ON canonical.%I '
            'FOR EACH ROW EXECUTE FUNCTION canonical.block_delete()',
            table_name
        );
    END LOOP;
END;
$$;

CREATE TABLE research.episodes (
    organization_id text NOT NULL,
    episode_id text NOT NULL,
    user_id text NOT NULL,
    conversation_id text NOT NULL,
    question text NOT NULL CHECK (btrim(question) <> ''),
    applicable_time date NOT NULL,
    document_scope jsonb NOT NULL DEFAULT '[]'::jsonb,
    identified_issues jsonb NOT NULL DEFAULT '[]'::jsonb,
    actions jsonb NOT NULL DEFAULT '[]'::jsonb,
    visited_nodes jsonb NOT NULL DEFAULT '[]'::jsonb,
    visited_provisions jsonb NOT NULL DEFAULT '[]'::jsonb,
    answer_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    PRIMARY KEY (organization_id, episode_id)
);

CREATE INDEX ix_research_episodes_time
    ON research.episodes (organization_id, created_at DESC);

CREATE TABLE research.evidence (
    organization_id text NOT NULL,
    evidence_id text NOT NULL,
    episode_id text NOT NULL,
    document_id text NOT NULL,
    document_version_id text NOT NULL REFERENCES canonical.document_versions(document_version_id),
    provision_id text NOT NULL REFERENCES canonical.provisions(provision_id),
    provision_version_id text NOT NULL REFERENCES canonical.provision_versions(provision_version_id),
    source_span_id text NOT NULL REFERENCES canonical.source_spans(source_span_id),
    page integer NOT NULL CHECK (page >= 1),
    text text NOT NULL CHECK (btrim(text) <> ''),
    retrieval_method text NOT NULL CHECK (btrim(retrieval_method) <> ''),
    reason_selected text NOT NULL CHECK (btrim(reason_selected) <> ''),
    applicable_time date NOT NULL,
    confidence double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, evidence_id),
    FOREIGN KEY (organization_id, episode_id)
        REFERENCES research.episodes(organization_id, episode_id)
);

CREATE INDEX ix_evidence_episode ON research.evidence (organization_id, episode_id);

CREATE TABLE research.progressive_relations (
    organization_id text NOT NULL,
    relation_id text NOT NULL,
    source_namespace text NOT NULL CHECK (source_namespace IN ('canonical', 'research')),
    source_node_id text NOT NULL CHECK (btrim(source_node_id) <> ''),
    target_namespace text NOT NULL CHECK (target_namespace IN ('canonical', 'research')),
    target_node_id text NOT NULL CHECK (btrim(target_node_id) <> ''),
    edge_type text NOT NULL CHECK (btrim(edge_type) <> ''),
    truth_class text NOT NULL CHECK (truth_class IN ('explicit', 'deterministic', 'inferred', 'expert_asserted')),
    status text NOT NULL CHECK (
        status IN ('candidate', 'expert_approved', 'rejected', 'needs_revalidation', 'invalidated', 'archived')
    ),
    source_episode_id text NOT NULL,
    confidence double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    approved_by text,
    approved_at timestamptz,
    approval_note text,
    approval_organization_id text,
    generated_by_model text,
    model_profile text,
    prompt_version text,
    agent_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    PRIMARY KEY (organization_id, relation_id),
    FOREIGN KEY (organization_id, source_episode_id)
        REFERENCES research.episodes(organization_id, episode_id),
    CHECK (source_namespace <> target_namespace OR source_node_id <> target_node_id),
    CHECK (
        status <> 'expert_approved'
        OR (approved_by IS NOT NULL AND approved_at IS NOT NULL AND approval_organization_id = organization_id)
    ),
    CHECK (
        status NOT IN ('candidate', 'rejected')
        OR (approved_by IS NULL AND approved_at IS NULL AND approval_organization_id IS NULL)
    )
);

CREATE INDEX ix_progressive_relations_navigation
    ON research.progressive_relations (organization_id, source_node_id, edge_type)
    WHERE status = 'expert_approved';

CREATE TABLE research.relation_evidence (
    organization_id text NOT NULL,
    relation_id text NOT NULL,
    provision_version_id text NOT NULL REFERENCES canonical.provision_versions(provision_version_id),
    evidence_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, relation_id, provision_version_id),
    FOREIGN KEY (organization_id, relation_id)
        REFERENCES research.progressive_relations(organization_id, relation_id),
    FOREIGN KEY (organization_id, evidence_id)
        REFERENCES research.evidence(organization_id, evidence_id)
);

CREATE INDEX ix_relation_evidence_source_version
    ON research.relation_evidence (provision_version_id, organization_id);

CREATE TABLE research.memory_events (
    organization_id text NOT NULL,
    event_id text NOT NULL,
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    action text NOT NULL CHECK (
        action IN (
            'candidate_created', 'candidate_edited', 'candidate_approved', 'candidate_rejected',
            'knowledge_revalidated', 'knowledge_invalidated', 'knowledge_archived', 'knowledge_restored'
        )
    ),
    target_id text NOT NULL,
    previous_state text,
    new_state text NOT NULL,
    reason text,
    source_episode_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (organization_id, event_id),
    FOREIGN KEY (organization_id, target_id)
        REFERENCES research.progressive_relations(organization_id, relation_id),
    FOREIGN KEY (organization_id, source_episode_id)
        REFERENCES research.episodes(organization_id, episode_id),
    CHECK (
        previous_state IS NULL OR previous_state IN (
            'candidate', 'expert_approved', 'rejected', 'needs_revalidation', 'invalidated', 'archived'
        )
    ),
    CHECK (
        new_state IN (
            'candidate', 'expert_approved', 'rejected', 'needs_revalidation', 'invalidated', 'archived'
        )
    )
);

CREATE INDEX ix_memory_events_rebuild
    ON research.memory_events (organization_id, occurred_at, event_id);

CREATE OR REPLACE FUNCTION research.ensure_interpretive_evidence()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_organization_id text := COALESCE(NEW.organization_id, OLD.organization_id);
    target_relation_id text := COALESCE(NEW.relation_id, OLD.relation_id);
    relation_truth_class text;
BEGIN
    SELECT truth_class INTO relation_truth_class
    FROM research.progressive_relations
    WHERE organization_id = target_organization_id AND relation_id = target_relation_id;

    IF relation_truth_class IN ('inferred', 'expert_asserted')
       AND NOT EXISTS (
           SELECT 1 FROM research.relation_evidence
           WHERE organization_id = target_organization_id AND relation_id = target_relation_id
       ) THEN
        RAISE EXCEPTION 'interpretive relation % requires versioned evidence', target_relation_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER progressive_relation_requires_evidence
    AFTER INSERT OR UPDATE OF truth_class ON research.progressive_relations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION research.ensure_interpretive_evidence();

CREATE CONSTRAINT TRIGGER relation_evidence_cannot_orphan_interpretation
    AFTER DELETE OR UPDATE ON research.relation_evidence
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION research.ensure_interpretive_evidence();

CREATE OR REPLACE FUNCTION research.protect_progressive_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.relation_id IS DISTINCT FROM OLD.relation_id
       OR NEW.source_episode_id IS DISTINCT FROM OLD.source_episode_id THEN
        RAISE EXCEPTION 'progressive knowledge identity and provenance are immutable';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER protect_progressive_identity
    BEFORE UPDATE ON research.progressive_relations
    FOR EACH ROW EXECUTE FUNCTION research.protect_progressive_identity();

CREATE OR REPLACE FUNCTION research.block_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit history is append-only';
END;
$$;

CREATE TRIGGER block_memory_event_update_delete
    BEFORE UPDATE OR DELETE ON research.memory_events
    FOR EACH ROW EXECUTE FUNCTION research.block_audit_mutation();

CREATE TRIGGER block_progressive_relation_delete
    BEFORE DELETE ON research.progressive_relations
    FOR EACH ROW EXECUTE FUNCTION research.block_audit_mutation();

CREATE TRIGGER block_episode_delete
    BEFORE DELETE ON research.episodes
    FOR EACH ROW EXECUTE FUNCTION research.block_audit_mutation();
CREATE TRIGGER block_evidence_delete
    BEFORE DELETE ON research.evidence
    FOR EACH ROW EXECUTE FUNCTION research.block_audit_mutation();
CREATE TRIGGER block_relation_evidence_delete
    BEFORE DELETE ON research.relation_evidence
    FOR EACH ROW EXECUTE FUNCTION research.block_audit_mutation();

CREATE OR REPLACE FUNCTION research.current_organization_id()
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('legal_agent.organization_id', true), '');
$$;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['episodes', 'evidence', 'progressive_relations', 'relation_evidence', 'memory_events']
    LOOP
        EXECUTE format('ALTER TABLE research.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON research.%I '
            'USING (organization_id = research.current_organization_id()) '
            'WITH CHECK (organization_id = research.current_organization_id())',
            table_name
        );
    END LOOP;
END;
$$;

COMMENT ON SCHEMA canonical IS 'Source-backed, rebuildable legal corpus and graph; progressive memory must not overwrite it.';
COMMENT ON SCHEMA research IS 'Organization-scoped research episodes, evidence, approved navigation memory, and audit events.';
COMMENT ON COLUMN canonical.provision_versions.text IS 'Immutable source-near legal text; corrections create a new version.';
COMMENT ON COLUMN research.progressive_relations.status IS 'Only expert_approved rows are active navigation memory.';
