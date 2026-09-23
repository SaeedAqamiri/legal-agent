# Tamin Long-Document Agentic Benchmark v4

این Benchmark بر پایه ۹ PDF بلند بارگذاری‌شده از مقررات و بخشنامه‌های سازمان تأمین اجتماعی ساخته شده است.

## تغییر مهم v4

- متن همه سؤال‌ها به سبک پرسش واقعی حقوقی بازبینی شد.
- هیچ سؤال Blind اشاره‌ای به Agent، Corpus، Graph، Memory یا مسیر تحقیق ندارد.
- `questions.jsonl` هیچ evidence / required_documents / expected_navigation لو نمی‌دهد.
- مسیر پژوهش، temporal checks و evidence gap فقط در فایل‌های Gold/Evaluator هستند.
- ۶ سؤال مستقل **Temporal / Amendment / Version** افزوده شد.
- ۶ سؤال مستقل **Limited Evidence / Abstention** افزوده شد.

## اندازه مجموعه

- Documents: 9
- Questions: 76
- Direct: 13
- Intra-document: 14
- Cross-document: 13
- Agentic multi-hop: 10
- Long case-style: 10
- Adversarial: 4
- Temporal / Amendment / Version: 6
- Limited Evidence / Abstention: 6

## فایل‌های اصلی

- `benchmark/questions.jsonl`: ورودی Blind؛ فقط سؤال و metadata عمومی
- `benchmark/blind_questions.md`: نسخه خوانا برای تست دستی
- `benchmark/gold_answers.jsonl`: پاسخ حقوقی + metadata ارزیابی
- `benchmark/evaluator_metadata.jsonl`: فقط اطلاعات evaluator
- `benchmark/temporal_version_questions.jsonl`: سؤال‌های زمان/نسخه
- `benchmark/limited_evidence_questions.jsonl`: سؤال‌های پاسخ محدود/امتناع
- `benchmark/question_style_guide.md`: قرارداد نگارش سؤال
- `benchmark/evaluation_rubric.md`: روش امتیازدهی
- `documents_pdf/` و `documents_md/`: PDF و Markdown متناظر

## نکته مهم

در سؤال‌های تاریخی، «سند جدیدتر» لزوماً سند قابل اعمال بر واقعه قدیمی نیست. اگر نسخه تاریخی لازم در Corpus وجود نداشته باشد، پاسخ صحیح می‌تواند محدود یا غیرقطعی باشد. این رفتار failure نیست؛ بخشی از Benchmark است.
