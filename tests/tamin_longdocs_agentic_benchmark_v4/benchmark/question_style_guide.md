# Question Style Policy — v4

سؤال‌های Benchmark باید شبیه پرسش واقعی موکل، کارفرما، بیمه‌شده، مستمری‌بگیر یا وکیل پرونده باشند.

در متن سؤال ممنوع است:

- اشاره به Agent / RAG / Graph / Memory / Corpus / Benchmark
- گفتن اینکه «اول سند X را بخوان» یا «این مسیر را طی کن»
- افشای required_documents یا expected_navigation
- دستور ارزیابی مانند «طبق Corpus جواب بده»

این اطلاعات فقط در `gold_answers.jsonl` و `evaluator_metadata.jsonl` نگهداری می‌شوند.

پاسخ Gold نیز باید ابتدا یک پاسخ حقوقی طبیعی باشد. نکات مربوط به رفتار Agent، مسیر navigation، abstention و temporal checks در metadata جداگانه ثبت می‌شوند.
