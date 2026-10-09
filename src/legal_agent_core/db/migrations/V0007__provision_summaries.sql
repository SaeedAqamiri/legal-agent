-- Summaries for container nodes of the per-instrument structure trees
-- (بخش/فصل/...). Written offline by scripts/summarize_tree_nodes.py
-- (glm-5.3-flash), read at query time by the get_document_structure tool via
-- the lazy loader in composition.py and merged into out/document_trees/*.json
-- by scripts/build_document_tree_index.py.

CREATE TABLE canonical.provision_summaries (
    provision_id   text PRIMARY KEY REFERENCES canonical.provisions(provision_id) ON DELETE CASCADE,
    instrument_id  text NOT NULL REFERENCES canonical.legal_instruments(instrument_id) ON DELETE CASCADE,
    summary        text NOT NULL CHECK (btrim(summary) <> ''),
    model          text NOT NULL CHECK (btrim(model) <> ''),
    prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_provision_summaries_instrument
    ON canonical.provision_summaries (instrument_id);
