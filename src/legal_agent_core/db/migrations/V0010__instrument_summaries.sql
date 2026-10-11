-- One-line Persian summary per legal instrument (law/regulation/circular),
-- parallel to canonical.provision_summaries (PageIndex-style digests).
CREATE TABLE canonical.instrument_summaries (
    instrument_id  text PRIMARY KEY REFERENCES canonical.legal_instruments (instrument_id),
    summary        text NOT NULL CHECK (btrim(summary) <> ''),
    model          text,
    prompt_version text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
