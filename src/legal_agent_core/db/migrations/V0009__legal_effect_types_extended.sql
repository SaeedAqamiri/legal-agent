-- Widen the legal effect vocabulary so LLM-derived statutory-basis and
-- judicial candidates can flow through the existing review queue
-- (effect review publishes them as implements/interprets/conflicts_with edges).
ALTER TABLE sources.legal_effects DROP CONSTRAINT legal_effects_effect_type_check;
ALTER TABLE sources.legal_effects ADD CONSTRAINT legal_effects_effect_type_check
    CHECK (effect_type IN (
        'amend', 'append', 'supplement', 'repeal', 'replace',
        'suspend', 'restore', 'annul', 'implement', 'interpret', 'conflict'
    ));
