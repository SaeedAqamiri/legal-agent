-- Distinguish WHAT each canonical edge claims (legal relation vs display
-- heuristic vs site-curated link) from HOW it was validated. Design doc:
-- docs/qavanin/06_law_notebook.md §2 — a folder/derivation choice must never
-- silently become a legal claim; consumers (agent tools/UI) must surface
-- claim + review_status next to every edge.
--
-- claim:
--   statutory_basis  — implements («این آیین‌نامه مجری/مبتنی بر این قانون است»)
--   citation         — explicitly_references / cites (ارجاع یا استناد متنی)
--   amendment        — amends / repeals (تغییر وضعیت حقوقی)
--   site_related     — related_to (پیوند منتخب گردانندهٔ سایت؛ نه اجرا نه اصلاح)
--   display_hint     — subordinate_to (حدس سازمان‌دهی دامنه+tier؛ یال حقوقی نیست)
--
-- review_status (اعتبارِ بررسی، مستقل از روش استخراج):
--   unreviewed   — parser/derived منتشرشده بدون بازبینی انسانی
--   llm_inferred — از خروجی LLM منتشر شده (نیازمند بازبینی)
--   curated      — انتخاب انسانی/گردانندهٔ منبع
--   invalidated  — باطل‌شده (فقط برای گزارش؛ در پروجکشن نمایش داده نمی‌شود)

ALTER TABLE canonical.graph_edges ADD COLUMN IF NOT EXISTS claim text;
ALTER TABLE canonical.graph_edges ADD COLUMN IF NOT EXISTS review_status text;

UPDATE canonical.graph_edges
   SET claim = CASE edge_type
                   WHEN 'implements' THEN 'statutory_basis'
                   WHEN 'explicitly_references' THEN 'citation'
                   WHEN 'cites' THEN 'citation'
                   WHEN 'amends' THEN 'amendment'
                   WHEN 'repeals' THEN 'amendment'
                   WHEN 'related_to' THEN 'site_related'
                   WHEN 'subordinate_to' THEN 'display_hint'
                   ELSE NULL
               END,
       review_status = CASE
                   WHEN created_by = 'domain-hierarchy' THEN 'unreviewed'
                   WHEN created_by IN ('refs-llm', 'refs-llm-repair', 'effects-batch') THEN 'llm_inferred'
                   WHEN created_by = 'source-curation' THEN 'curated'
                   WHEN created_by = 'refs-invalidated' THEN 'invalidated'
                   ELSE 'unreviewed'
               END
 WHERE claim IS NULL;

CREATE INDEX IF NOT EXISTS ix_graph_edges_claim
    ON canonical.graph_edges (claim);
