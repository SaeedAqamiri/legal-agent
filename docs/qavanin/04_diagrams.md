# ۰۴ — نمودارها (Mermaid)

چهار نما: ۱) اسکیمای گراف ۲) یال‌های رابطه‌ای ۳) دیتابیس رابطه‌ای `sources.*` ۴) معماری سه‌لایه و خط تولید.

## ۱. گراف دانش — اسکیمای نودها

```mermaid
flowchart LR
    subgraph ENV["محیط"]
        J["Jurisdiction<br/>code · name"]
        D["LegalDomain<br/>name · درختی"]
        AU["Authority<br/>name · kind · tier"]
    end
    subgraph DOC["اسناد"]
        LI["LegalInstrument<br/>type · tier · status · aliases"]
        DV["DocumentVersion<br/>enacted · effective_from/to"]
        SD["SourceDocument<br/>uri · checksum · method"]
        SS["SourceSpan<br/>page · bbox"]
    end
    subgraph CONT["محتوا"]
        P["Provision<br/>type · number · depth"]
        PV["ProvisionVersion<br/>text · normalized<br/>effective_from/to"]
        T["Topic (فاز ۲)<br/>name · definition"]
    end

    AU -->|ISSUES| LI
    LI -->|BELONGS_TO_DOMAIN| D
    LI -->|BELONGS_TO_JURISDICTION| J
    LI -->|HAS_VERSION| DV
    DV -->|VERSION_OF| LI
    DV -.->|DERIVED_FROM| DV
    SD -.->|پشتیبان فیزیکی| DV
    DV -->|HAS_SPAN| SS
    SS -->|EVIDENCE_OF| PV
    LI -->|CONTAINS| P
    P -->|CONTAINS تودرتو| P
    P -->|HAS_VERSION| PV
    T -.-|INTERPRETED_AS| P
```

> واحد بازیابی برای LLM همین `ProvisionVersion` است (نه کل قانون): جستجوی واژگانی + برداری،
> سپس پالایش با بازه اعتبار و پیمایش روابط.

## ۲. گراف دانش — یال‌های رابطه‌ای میان اسناد (نسخ/ناسخ و سلسله‌مراتب)

```mermaid
flowchart LR
    REG["LegalInstrument<br/>آیین‌نامه / تصویب‌نامه (tier 4-5)"]
    LAW["LegalInstrument<br/>قانون عادی (tier 3)"]
    CONST["LegalInstrument<br/>قانون اساسی (tier 1)"]
    SPEC["LegalInstrument<br/>قوانین خاص — مجمع/شوراها (tier 2)"]
    DV["DocumentVersion<br/>نسخهٔ اصلاحی"]
    P1["ProvisionVersion<br/>مادهٔ نسخ‌شده"]
    P2["ProvisionVersion<br/>مادهٔ ناسخ"]

    REG ==>|"IMPLEMENTS<br/>authority_basis · review_status"| LAW
    LAW ==>|"IMPLEMENTS"| SPEC
    LAW ==>|"استناد اصل"| CONST
    DV -->|"AMENDS (اصلاح/الحاق)<br/>evidence_span"| LAW
    DV -.->|"REPEALS mode=implicit<br/>کاندید — بازبینی لازم"| LAW
    P2 ==>|"REPEALS mode=explicit scope=partial<br/>quote + page اجباری"| P1
    P1 -.->|"CONFLICTS_WITH (کاندید)"| P2
```

قاعدهٔ جهت‌دار: هر یال فقط **یک جهت** ذخیره می‌شود (`REPEALS`)؛ معکوس (`REPEALED_BY`) در زمان پرس‌وجو خوانده می‌شود — ذخیرهٔ هر دو، خطر ناسازگاری دارد.

## ۳. دیتابیس رابطه‌ای — `sources.*`

```mermaid
erDiagram
    INSTRUMENTS ||--o{ SOURCE_DOCUMENTS : "رکوردهای چندمنبعی"
    SOURCES ||--o{ SOURCE_DOCUMENTS : "دریافت"
    SOURCES ||--o{ FETCH_RUNS : "اجرا"
    SOURCE_DOCUMENTS ||--o{ EXTRACTION_JOBS : "استخراج"
    SOURCE_DOCUMENTS ||--o{ LEGAL_EFFECTS : "سند اثرگذار"
    INSTRUMENTS ||--o{ LEGAL_EFFECTS : "قانون اثرپذیر"

    INSTRUMENTS {
        uuid instrument_uid PK
        text canonical_key UK "نرمال‌شده از عنوان"
        text canonical_title
        text instrument_type
        smallint tier "1..5"
        text issuer
        text legal_status "کش عملیاتی"
        jsonb external_ids "شناسه در هر منبع"
    }

    SOURCES {
        text source_id PK
        text name
        text kind "portal|api|baray_form|upload|rss"
        text base_url
        boolean enabled
        jsonb checkpoint "کرسر ادامه دریافت"
    }

    SOURCE_DOCUMENTS {
        uuid document_uid PK
        text source_id FK
        text external_id "شناسه در سامانه مبدأ"
        uuid instrument_uid FK "پس از تطبیق"
        text match_status "unmatched|auto|expert"
        text source_uri "لینک ردیابی"
        text doc_kind "constitution..circular"
        smallint tier "سلسله‌مراتب 1..5"
        text title
        text origin_organization "مرجع وضع"
        text subject_group "گروه موضوع"
        text scope "دامنه شمول"
        text keywords "کلیدواژه‌ها - GIN"
        text instrument_no
        date issue_date "تصویب"
        date approval_date "تأیید شورای نگهبان و غیره"
        date notify_date "ابلاغ"
        date publication_date "انتشار"
        text gazette_no "شماره روزنامه رسمی"
        date gazette_date
        text gazette_page
        date effective_date "لازم‌الاجرا - قاعده ماده ۲ ق م"
        date expiry_date "پایان اعتبار"
        text original_path "اصل PDF - تغییرناپذیر"
        text original_checksum "sha256 - idempotency"
        integer page_count
        text text_path "متن markdown"
        text text_extract_method "pdftotext|vlm_ocr|html|manual"
        text ocr_model
        text fetch_state "new..published"
        text legal_status "کش - مرجع گراف است"
    }

    LEGAL_EFFECTS {
        uuid effect_uid PK
        uuid affecting_document_uid FK "سند اثرگذار"
        uuid affected_instrument_uid FK "قانون اثرپذیر"
        text affected_provision_label "ماده یا تبصره هدف"
        text effect_type "amend|append|repeal|suspend|annul|.."
        text mode "explicit|implicit"
        text scope "total|partial"
        date effective_at
        text witness_quote "شاهد متنی اجباری"
        integer witness_page
        text witness_checksum
        text detected_by "text_rule|llm|expert|import"
        text review_status "candidate|approved|rejected"
    }

    FETCH_RUNS {
        uuid run_id PK
        text source_id FK
        text status "running|succeeded|failed"
        jsonb checkpoint
        jsonb stats
    }

    EXTRACTION_JOBS {
        uuid job_id PK
        uuid document_uid FK
        text stage "enrich|references|amendment|conflict"
        text model
        text prompt_version
        text input_checksum
        jsonb output "خروجی ساخت‌یافته"
        text review_status "candidate|approved|rejected"
        text reviewed_by
    }
```

جریان انتشار: `legal_effects (approved)` منشأ یال‌های `AMENDS/REPEALS` گراف است؛ `instruments` هدف نشر به `(:LegalInstrument)`. ساختار مواد و «وضعیت در تاریخ X» عمداً فقط در گراف زندگی می‌کند تا یک منبع حقیقت داشته باشیم.

## ۴. معماری اصلی — سه لایه و خط تولید

```mermaid
flowchart TB
    subgraph SRC["منابع S0 — کانکتورها (checkpoint دار)"]
        direction LR
        QAV["qavanin.ir"]
        DOT["dotic.ir"]
        RRK["rrk.ir"]
        RCM["rc.majlis.ir"]
        BAR["بارای ۹۰۰۰"]
        UP["فایل کاربر"]
    end

    subgraph PIPE["خط تولید"]
        direction TB
        F["S1 دریافت<br/>checksum + idempotent"] --> TX["S2 متن<br/>pdftotext → در صورت خرابی VLM OCR"]
        TX --> PAR["S3 ساختار<br/>پارسر Provision (بدون LLM)"]
        PAR --> LLM["S4 غنی‌سازی LLM<br/>متادیتا · تطبیق · نسخ/ناسخ · تعارض"]
        LLM --> GATE["S5 درگاه قواعد<br/>tier · تاریخ‌ها · شاهدِ نقل‌قول"]
        GATE --> REV["S6 بازبینی انسان<br/>candidate → approved"]
        REV --> PUB["S7 انتشار"]
    end

    subgraph STORE["لایه ۱ — انبار محتوا"]
        FS[("data/qavanin_store<br/>originals · text")]
        PG[("Postgres sources.*<br/>instruments · source_documents<br/>legal_effects · fetch_runs · extraction_jobs")]
    end

    subgraph KNOW["لایه ۲ — گراف دانش"]
        FK[("FalkorDB<br/>LegalInstrument · Provision<br/>IMPLEMENTS · REPEALS")]
    end

    APP["عامل پژوهش حقوقی<br/>پاسخ: نام قانون · ماده · نسخه معتبر<br/>تاریخ مبنا · پیوند شاهد"]

    SRC --> F
    F --> FS
    F --> PG
    TX --> FS
    TX --> PG
    LLM --> PG
    PUB --> PG
    PUB --> FK
    PG -.->|"instruments + effects تأییدشده"| FK
    FK --> APP
    FS -.->|"شواهد و اصل اسناد"| APP
```

قاعدهٔ سراسری: هیچ داده‌ای بدون گذر از S5 و S6 وارد گراف قطعی نمی‌شود؛ اصل فایل همیشه قبل از هر پردازش LLM در لایه ۱ ذخیره شده است؛ پاسخ نهایی بدون «نام قانون + ماده + نسخه + تاریخ مبنا + شاهد» منتشر نمی‌شود.
