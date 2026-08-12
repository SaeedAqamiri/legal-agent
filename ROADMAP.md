# Roadmap

این roadmap سطح کلان پروژه را نشان می‌دهد و با پیشرفت ADRها به‌روزرسانی می‌شود.

## Backend MVP — انجام‌شده

1. Canonical domain model و invariantهای ADR-001/002/003
2. PostgreSQL schema و migration runner
3. Progressive Research Graph و FalkorDB adapter
4. Canonical ingestion، stable identity و explicit-reference extraction
5. Evidence Ledger، Evidence Verifier و Navigation Loop
6. Multi-signal Retrieval با temporal/scope gate، ranking قابل توضیح و graph expansion
7. LLM Gateway و Model Profiles با prompt/agent versioning، retry/timeout و providerهای قابل تعویض
8. Answer Composer و Citation Pipeline با claim-to-evidence map و publish gate
9. Production Persistence و Ingestion Operations با PostgreSQL repository، UoW، job retry و correction/amendment workflow
10. REST API، tenant-aware authorization، expert-review endpoints، evaluation harness، metrics و API E2E tests

## Product UI — در حال انجام

انجام‌شده:

- Research Workspace فارسی و واکنش‌گرا
- اجرای سؤال با `applicable_time` و document scope
- نمایش پاسخ، Claimها و citation دقیق نسخه/ماده/span/صفحه
- نمایش Research Trace و Evidence Ledger
- صف بازبینی tenant-aware و جریان تأیید/رد کارشناس
- CSP و security headers، DOM rendering امن و تست دسکتاپ/موبایل

باقی‌مانده در این گام:

- Library اسناد، جست‌وجو و فیلتر مجموعه قوانین
- document/PDF viewer با highlight دقیق SourceSpan
- تاریخچه پرونده‌های پژوهشی و بازیابی بین sessionها
- explainability کامل Progressive Memory و نمایش dependency graph
- اتصال login به Identity Provider واقعی به‌جای ورود دستی token

## Production Operations و Governance Hardening

- deployment و environment configuration
- external identity provider و secret management
- backup/restore و rollback
- tenant administration، retention و audit export
- load testing، accessibility audit و longitudinal evaluation

اتصال‌های زنده PostgreSQL، FalkorDB و LLM در تست‌های integration به‌صورت opt-in باقی می‌مانند؛ چون اجرای آن‌ها به سرویس و credential محیط مقصد نیاز دارد.
