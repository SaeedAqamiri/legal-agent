# data/qavanin_store — انبار محتوای قوانین

«منبع حقیقتِ محتوا»: اصل فایل‌ها و متن‌ها اینجا زندگی می‌کنند؛ دیتابیس (`sources.*`) و گراف فقط به این مسیرها ارجاع می‌دهند.

```
qavanin_store/
├── README.md              ← همین فایل
├── sources.json           ← رجیستری منابع (سنگ‌بنای sources.sources در دیتابیس)
├── originals/<source_id>/<external_id|uid>/   ← اصل فایل (pdf/html/docx) — هرگز تغییر نمی‌کند
│   └── pages/             ← تصویر صفحات (خروجی pdftoppm برای OCR/بازبینی)
├── text/<source_id>/<external_id|uid>.md      ← متن تبدیل‌شده (markdown با «## صفحه N»)
├── discovery/             ← خروجی کشف بارای (systems/forms/endpoints)
└── staging/               ← فایل‌های دستی/موارد استثنا قبل از ورود به جریان
```

قواعد:
1. **اثر انگشت**: هر فایل با `sha256` در `sources.source_documents` ثبت می‌شود؛ فایل هم‌چک‌سام دوباره وارد نمی‌شود (idempotent).
2. **دوگانه اصل/متن**: `originals/` تغییرناپذیر است؛ `text/` بازتولیدشدنی است و `text_extract_method` (pdftotext/vlm_ocr/…) ثبت می‌شود.
3. نام‌گذاری: `originals/<source_id>/<external_id>.pdf` و اگر external_id نبود، `document_uid`.

بارگذاری اولیه: کورپوس پایه ۱۳ سند (قوانین بالادستی) در همین چیدمان منتقل و سپس به گراف منتشر می‌شود.
