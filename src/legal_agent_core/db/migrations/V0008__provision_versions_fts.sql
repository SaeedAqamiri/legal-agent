-- Full-text prefilter for lexical search (PageIndex-adjacent retrieval fix).
-- The research tools used to scan every provision version per call; this
-- generated tsvector + GIN index lets the Postgres adapter return a ranked
-- candidate set that the tools score exactly in Python afterwards.

ALTER TABLE canonical.provision_versions
    ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', COALESCE(normalized_text, ''))) STORED;

CREATE INDEX ix_provision_versions_tsv
    ON canonical.provision_versions USING GIN (tsv);
