-- V0006 — cats corpus vocabulary (ekhtebar cats/*: rulings, advisory opinions,
-- policies, judicial sessions)
--
-- Widens the operational vocabularies so the cats corpus can register:
--   * advisory_opinion — نظریه‌های مشورتی اداره کل حقوقی / نظریه و اعلام مغایرت رئیس مجلس
--   * policy           — سیاست‌های کلی نظام (ابلاغی مقام معظم رهبری)
--   * guideline        — نشست‌های قضایی (رویکرد یکدست شعب)
--   * interpret/conflict effects — وحدت رویه (تفسیر الزام‌آور مواد) و اعلام
--     مغایرت مصوبه با قانون؛ ابطال مصوبات با رأی دیوان عدالت اداری از قبل
--     با effect_type='annul' پوشش داده می‌شود.
-- Canonical graph edges (canonical.graph_edges.edge_type) are already free
-- text; this migration widens only the staging CHECK constraints in sources.

ALTER TABLE sources.instruments
    DROP CONSTRAINT IF EXISTS instruments_instrument_type_check;
ALTER TABLE sources.instruments
    ADD CONSTRAINT instruments_instrument_type_check
    CHECK (instrument_type IN (
        'constitution', 'statute', 'special_statute', 'regulation',
        'cabinet_approval', 'bylaw', 'circular', 'directive',
        'judgment', 'bill', 'advisory_opinion', 'policy', 'guideline',
        'unknown'));

ALTER TABLE sources.source_documents
    DROP CONSTRAINT IF EXISTS source_documents_doc_kind_check;
ALTER TABLE sources.source_documents
    ADD CONSTRAINT source_documents_doc_kind_check
    CHECK (doc_kind IN (
        'constitution', 'statute', 'special_statute', 'regulation',
        'cabinet_approval', 'bylaw', 'circular', 'directive',
        'judgment', 'bill', 'advisory_opinion', 'policy', 'guideline',
        'unknown'));

ALTER TABLE sources.legal_effects
    DROP CONSTRAINT IF EXISTS legal_effects_effect_type_check;
ALTER TABLE sources.legal_effects
    ADD CONSTRAINT legal_effects_effect_type_check
    CHECK (effect_type IN (
        'amend', 'append', 'supplement', 'repeal', 'replace',
        'suspend', 'restore', 'annul', 'interpret', 'conflict'));
