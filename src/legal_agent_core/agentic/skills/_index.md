# Skills — procedural knowledge injected into the planner prompt.

Skills are GUIDANCE ONLY, never an authorization source (the tool surface is
fixed by ``LegalResearchTools``).

## Files
- `deadline_appeal.md` — مهلت‌ها و اعتراضات: یافتن مهلت، کنترل نسخه، جداکردن استثناها
- `temporal_version.md` — نسخه‌ی زمانی: واقعه/نسخه، امتناع در نبود نسخه، منع retroactive
- `cross_reference.md` — زنجیره‌ی ارجاع صریح با get_related
- `navigate_document.md` — ناوبری درون سند: list/find/read و cite همان که خوانده شد

## Routing rule (v1, simple & testable)
Keyword router picks ONE starting skill per question; the planner may deviate
freely. Semantic routing only if the benchmark demands it.
