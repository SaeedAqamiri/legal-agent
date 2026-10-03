# ۰۳ — پایپ‌لاین ساخت گراف و دیتابیس از اسناد با LLM

> الگو: خط تولید OpenCTI — «کانکتور → صف/کار → پارس/غنی‌سازی → درگاه اعتبارسنجی → انتشار» + لایه کاندید برای خروجی LLM.

## ۰. نمای کلی جریان

```
 [S0 منابع]        dotic.ir / qavanin.ir / rrk.ir / rc.majlis.ir / بارای-۹۰۰۰ / فایل‌های کاربر
      │  (کانکتورها: هرکدام checkpoint خودش، idempotent روی checksum)
      ▼
 [S1 دریافت]       اصل فایل → data/qavanin_store/originals/  + ردیف source_documents (fetch_state=fetched)
      ▼
 [S2 متن]          pdftotext → اگر خراب/خالی بود → VLM OCR (VLMOCRAdapter موجود)
      │            → data/qavanin_store/text/  + text_path/method/checksum (text_extracted)
      ▼
 [S3 ساختار]       پارسر مارک‌داون v3 (بدون LLM): درخت Provision + صفحات (parsed)
      ▼
 [S4 غنی‌سازی LLM]  ۴ کار ساخت‌یافته، هرکدام یک extraction_job با prompt_version و خروجی JSON سخت‌گیرانه:
      │   a) metadata_enrich      → tier، issuer، doc_kind، تاریخ‌ها، aliases
      │   b) reference_resolution → «موضوع ماده ۵ قانون X» → پیوند به instrument/Provision
      │   c) amendment_detection  → AMENDS/REPEALS با mode صریح/ضمنی + نقل‌قول شاهد
      │   d) conflict_scan        → CONFLICTS_WITH کاندید (نسخ ضمنی/تخصیص)
      ▼
 [S5 درگاه اعتبار]  قواعد قطعی (تاریخ‌ها، tier مقرره ≤ قانون مادر، شاهدِ ناسخ، آستانه اطمینان)
      │            + قاضی LLM برای کاندیدهای مرزی → فقط passed به بازبینی می‌رود
      ▼
 [S6 بازبینی انسان] candidate → expert_approved (UI بازبینی موجود)
      ▼
 [S7 انتشار]        گراف FalkorDB (نود/یال با provenance) + fetch_state=published
```

قاعده ثابت: **اصل PDF و متن، قبل از هر LLM، ذخیره شده‌اند** — خروجی LLM هرگز تنها منبع نیست.

## ۱. کانکتورها (S0) — الگوی OpenCTI

هر منبع یک کانکتور با این قرارداد (`sources.sources` + `fetch_runs`):

```python
class SourceConnector(Protocol):
    source_id: str
    def fetch(self, checkpoint: dict) -> FetchRun: ...   # artifacts + checkpoint جدید
```

- `FetchRun` لیستی از `SourceArtifact` برمی‌گرداند (همان dataclass موجود در `ingestion/operations.py`).
- checkpoint = کرسر (تاریخ آخرین قانون، شماره صفحه، شناسه آخرین خبر) → اجرای بعدی ادامه می‌دهد.
- idempotency: `(source_id, sha256(content))` — فایل تکراری دوباره وارد نمی‌شود؛ `content_changed_at` تغییرها را ثبت می‌کند.
- منابع اولیه:
  | منبع | kind | نکته |
  |---|---|---|
  | qavanin.ir (TreeText) | portal | متن ساخت‌یافته + شماره ثبت |
  | dotic.ir (اخبار/سامانه) | portal | برای مقررات و آرای جدید |
  | rrk.ir | portal/rss | روزنامه رسمی (مرجع تاریخ انتشار) |
  | rc.majlis.ir | portal | آرشیو قوانین قدیمی (پشتیبان متن) |
  | بارای 192.168.8.41:9000 | baray_form | اسناد داخلی؛ پس از discovery کامل |
  | فایل‌های کاربر | upload | مسیر اصلی فاز بعد (فایل‌هایی که خودتان می‌دهید) |

## ۲. استخراج متن (S2) — pdftotext اول، VLM بعد

1. `pdftotext -layout` → اگر «chars/page < آستانه» یا «نسبت فارسی/خرابی بالا» → **VLM OCR**.
2. OCR با `VLMOCRAdapter` موجود (`adapters/vlm_ocr.py` + `scripts/vlm_ocr_corpus.py`): هر صفحه با `pdftoppm` تصویر و با پرامپت verbatim به مدل بینایی — خروجی markdown `## صفحه N` مستقیم قابل پارس.
3. هر دو حالت: `text_extract_method` و `ocr_model/confidence` در دیتابیس ثبت می‌شود؛ فایل تصویر صفحات حساس هم در `originals/<uid>/pages/` نگه داشته می‌شود تا بازبینی انسانی ممکن باشد.
4. من انجمن (agent) هم می‌توانم PDF/تصویر را مستقیم بخوانم — برای موارد استثنا (PDF قفل، جداول پیچیده) به‌صورت دستی به `staging/` می‌دهم و pipeline از همان ادامه می‌دهد.

## ۳. کارهای LLM (S4) — قراردادها

- **خروجی سخت‌گیرانه**: هر کار JSON با schema اعلام‌شده (`StructuredOutput` موجود در `llm.py`؛ strict/raw_decode مثل semantic.py). خروجی خراب = job failed، هرگز حدس در گراف.
- **هر خروجی حتماً شاهد دارد**: برای هر یال پیشنهادی، `quote` (نقل مستقیم از متن سند) + `page` الزامی است؛ quote با رشته‌یابی در متن تأیید می‌شود (وگرنه reject خودکار).
- **پرامپت‌ها نسخه‌دار**: `prompt_version` در جدول job — تغییر پرامپت = محاسبه مجدد قابل ردیابی.
- **کش هزینه**: input_checksum → اگر همان ورودی با همان prompt_version قبلاً موفق شده، skip.
- **chunking**: به‌جای کل قانون، per-provision / پنجره‌های ۳ تا ۵ ماده‌ای برای reference/amendment.

نمونه خروجی `amendment_detection`:
```json
{"relations": [{
   "type": "REPEALS", "mode": "implicit", "scope": "partial",
   "target_hint": "قانون حمایت از …", "target_provision": "ماده ۷",
   "quote": "…مقررات مغایر با این قانون ملغی است…", "page": 12,
   "confidence": 0.72 }]}
```

## ۴. درگاه اعتبارسنجی (S5) — قواعد قطعی

- tier مقرره (۴/۵) باید ≤ قانون مادری که به آن IMPLEMENTS می‌شود.
- تاریخ ناسخ ≥ تاریخ سند منسوخ؛ effective ranges معتبر (validate_period موجود).
- REPEALS بدون quote معتبر → reject. confidence زیر آستانه → فقط candidate.
- تعارض‌ها هرگز قطعی نمی‌شوند؛ فقط «کاندید بازبینی» تولید می‌کنند (نام enum موجود: `potentially_conflicts_with`).

## ۵. انتشار (S7) و شواهد

- گراف: نود/یال با `Provenance` کامل (همان dataclass canonical) — علاوه بر آن یال‌های LLM با `creation_method='llm_extractor'`, `model_id`, `prompt_version`.
- کتابخانه/جستجو: همان مسیر موجود (library + navigation/agentic tools)؛ یال‌های `IMPLEMENTS/REPEALS` بعداً در `get_related` ظاهر می‌شوند تا پاسخ‌دهنده بتواند قانون مادر/منسوخ را نشان دهد.

## ۶. چه چیزی الان آماده است و چه چیزی بعد از فایل‌های شما

| مرحله | وضعیت |
|---|---|
| VLM OCR (آداپتور + اسکریپت) | ✅ موجود (`adapters/vlm_ocr.py`, `scripts/vlm_ocr_corpus.py`) |
| پارسر ساختاری، ارجاع درون‌سندی، canonical pipeline | ✅ موجود |
| schema دیتابیس منابع (V0005) + مستندات | ✅ همین الان |
| انبار فایل‌ها (`data/qavanin_store`) + رجیستری منابع | ✅ همین الان |
| کانکتورهای پورتال (S0) | بعد از گرفتن فایل‌های شما و اولویت‌بندی منابع |
| کارهای LLM (S4) با promptهای نسخه‌دار | بعد از اولین دسته اسناد واقعی (تا پرامپت روی داده واقعی تنظیم شود) |
| نگاشت فرم‌های بارای | پس از اجرای `scripts/baray_explore.py` روی شبکه پایدار |
