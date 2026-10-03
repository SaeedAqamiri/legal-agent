# ۰۱ — طرح کامل گراف دانش حقوقی (FalkorDB)

> الهام‌گرفته از مدل STIX در OpenCTI: «نهادهای نوع‌دار با ویژگی‌های اعلانی + روابط نوع‌دار با provenance»،
> اما با مدل کنونیکال خودِ پروژه هم‌راستا (`src/legal_agent_core/canonical.py`).

## ۰. اصول طراحی

1. **دو سطح نهاد**: *نهاد حقوقی ثابت در زمان* (قانون به‌عنوان موجودیت حقوقی) و *نسخه زمانی* (اصلاحی/تنقیحی). این همان تفکیک OpenCTI بین entity و version است و سیر نسخ/ناسخ را ممکن می‌کند.
2. **هر یال provenance دارد**: چه کسی/با چه روشی/از کدام منبع/کِی (`created_by`, `creation_method`, `source_id`, `confidence`, `review_status`). هیچ یال LLMی بدون برچسب منبع نمی‌سازیم.
3. **دو لایه اعتبار**: یال‌های قطعی (parser/انسان) و یال‌های کاندید (LLM، با `review_status='candidate'`) — مطابق جریان candidate→expert_approved موجود.
4. **سلسله‌مراتب از قبل در enum ها هست**: `InstrumentType`، `CanonicalEdgeType.IMPLEMENTS/AMENDS/REPEALS/...` — این طرح فقط آن‌ها را پر می‌کند و چند نوع مکمل اضافه می‌کند.

---

## ۱. نودها (برچسب‌ها و ویژگی‌ها)

### ۱.۱ نودهای محیطی

| برچسب | نقش | ویژگی‌ها (کلید اصلی با *) |
|---|---|---|
| `(:Jurisdiction)` | حوزه قضایی | `jurisdiction_id*`, `code` ('IR'), `name` («جمهوری اسلامی ایران») |
| `(:LegalDomain)` | حوزه موضوعی (درختی) | `domain_id*`, `name` (مدنی، کیفری، ورزش، قضایی، …), `code`, `description` |
| `(:Authority)` | مرجع وضع (مجلس، هیئت وزیران، مقام رهبری، شورای عالی امنیت ملی، …) | `authority_id*`, `name`, `kind` (قوه مقننه/مجرای/قضاییه/رهبری/…)، `tier` (لایهٔ حکمرانی ۱..۵) |

### ۱.۲ نودهای اسناد

| برچسب | نقش | ویژگی‌ها |
|---|---|---|
| `(:LegalInstrument)` | **قانون/مقرره به‌عنوان نهاد حقوقی** (ثابت در زمان) | `instrument_id*`, `title`, `canonical_title`, `instrument_type` (constitution/statute/special_statute/regulation/cabinet_approval/bylaw/circular/directive/judgment), `tier` (۱..۵ — سلسله‌مراتب)، `issuer`, `subject_domain`, `external_id` (شمارهٔ qavanin/dotic), `status` (effective/amended/repealed/suspended), `aliases[]` (عناوین عامیانه برای جستجو) |
| `(:DocumentVersion)` | نسخهٔ مشخص سند (اصل مصوب، اصلاحی ۱۳۶۸، نسخهٔ تنقیحی…) | `document_version_id*`, `version_label`, `version_number`, `enacted_at`, `effective_from`, `effective_to`, `repealed_at`, `status`, `publication_ref` (روزنامه رسمی: شماره/تاریخ/صفحه) |
| `(:SourceDocument)` | فایل فیزیکی منبع | `source_document_id*`, `uri` (مسیر در `data/qavanin_store/`), `media_type` (pdf/markdown/html), `checksum`, `page_count`, `parser_version`, `extract_method` (pdftotext/vlm_ocr/provided) |
| `(:SourceSpan)` | مکان دقیق در منبع (شاهد متن) | `span_id*`, `page`, `bbox`, `text_start`, `text_end` |

### ۱.۳ نودهای محتوا

| برچسب | نقش | ویژگی‌ها |
|---|---|---|
| `(:Provision)` | واحد ساختاری حقوق (اصل/فصل/ماده/تبصره/بند) — ثابت در زمان | `provision_id*`, `provision_type` (constitution_article/book/part/chapter/section/article/note/clause/paragraph/…), `number` («۱»، «۴»، «۱ مکرر»), `label`, `ordinal`, `depth` |
| `(:ProvisionVersion)` | متن نسخه‌دارِ یک ماده | `provision_version_id*`, `text`, `normalized_text`, `status`, `effective_from`, `effective_to` |
| `(:Topic)` *(فاز ۲)* | مفهوم/موضوع معنایی (e.g. «مجوز», «عوارض») | `topic_id*`, `name`, `definition` — برای پرش‌های معنایی بین قوانین |

> نکته: `InstrumentType` فعلی enum مقدارهای `constitution/statute/regulation/...` را دارد؛ مقدارهای `special_statute` و `cabinet_approval` به enum اضافه می‌شوند تا طبقه ماده ۳ قانون تدوین و تنقیح دقیق مدل شود.

---

## ۲. یال‌ها (نوع، جهت، ویژگی)

### ۲.۱ ساختار سند

| یال | از → به | ویژگی‌ها | منشأ |
|---|---|---|---|
| `ISSUES` | Authority → LegalInstrument | — | متادیتا |
| `BELONGS_TO_DOMAIN` | LegalInstrument → LegalDomain | `primary: bool` | متادیتا/LLM |
| `BELONGS_TO_JURISDICTION` | LegalInstrument → Jurisdiction | — | ثابت |
| `HAS_VERSION` | LegalInstrument → DocumentVersion | `is_current: bool` | parser |
| `VERSION_OF` | DocumentVersion → LegalInstrument | — | parser |
| `DERIVED_FROM` | DocumentVersion → DocumentVersion | `kind` (consolidated/typified/re-ocr) | pipeline |
| `CONTAINS` | LegalInstrument/DocumentVersion/Provision → Provision | `ordinal` | parser |
| `HAS_VERSION` | Provision → ProvisionVersion | — | parser |
| `HAS_SPAN` | DocumentVersion/ProvisionVersion → SourceSpan | — | parser |
| `EVIDENCE_OF` | SourceSpan → ProvisionVersion | `confidence` | parser |

### ۲.۲ سلسله‌مراتب و رابطهٔ مقرره با قانون مادر

| یال | از → به | ویژگی‌ها | معنا |
|---|---|---|---|
| `IMPLEMENTS` | LegalInstrument (مقرره) → LegalInstrument (قانون/بالاتر) | `authority_basis` (مادهٔ احکام/اصل مجاز), `confidence`, `review_status` | «این آیین‌نامه مجریِ این قانون است» — ستون فقرات سلسله‌مراتب |
| `EXPLICITLY_REFERENCES` | Provision/Instrument → Provision/Instrument | `quote`, `confidence`, `resolution` (resolved/ambiguous/unresolved) | ارجاع صریح متن («موضوع ماده ۵ قانون …») |
| `CITES` | Judgment/Analysis → LegalInstrument/Provision | `role` (basis/interpretation) | استناد در رأی یا تحلیل (فاز ۲) |

### ۲.۳ نسخ و ناسخ (امضای این طرح)

| یال | از → به | ویژگی‌ها | معنا |
|---|---|---|---|
| `AMENDS` | DocumentVersion/LegalInstrument → LegalInstrument | `mode` (اصلاح/الحاق/تتمیم/یک‌ماده‌ای), `effective_at`, `evidence_span`, `review_status` | اصلاح/الحاق |
| `REPEALS` | LegalInstrument/DocumentVersion → LegalInstrument/Provision | `mode`: **`explicit`** (نسخ صریح با تصریح متن) یا **`implicit`** (نسخ ضمنی/تعارض ناسخ), `scope`: `total`/`partial`, `evidence_span`, `detected_by` (text_rule/llm/expert), `confidence`, `review_status` | نسخ |
| `SUSPENDS` / `RESTORES` / `REPLACES` / `SUPERSEDES` | مشابه بالا | همان الگو | توقف اجرا/ابطال آرای دیوان/جایگزینی/تنقیح |

قاعدهٔ طلایی: **هر `REPEALS` باید `evidence_span` داشته باشد** (نقل‌قول یا مکان در سندِ ناسخ)؛ نسخ ضمنی همیشه `mode='implicit'` و `review_status='candidate'` متولد می‌شود و فقط با تأیید کارشناس قطعی می‌شود.

### ۲.۴ لایهٔ تحلیلی (کاندید)

| یال | از → به | ویژگی‌ها |
|---|---|---|
| `CONFLICTS_WITH` | ProvisionVersion → ProvisionVersion | `kind` (تعارض/تخصیص/نسخ ضمنی), `rationale`, `confidence`, `review_status` |
| `INTERPRETED_AS` | Topic/Case → Provision | فاز ۲ |
| `COMMONLY_CHECK_WITH` | Provision → Provision | از حافظهٔ پیشرو موجود (`ProgressiveEdgeType`) |

### ۲.۵ ویژگی‌های مشترک همهٔ نودها و یال‌ها (منشأ OpenCTI)

```
provenance:    created_by, created_at, creation_method (parser|ocr|llm|human|import),
               source_id (SourceDocument), parser_version?, model_id?, prompt_version?
اعتبار:        review_status (candidate|expert_approved|rejected), confidence (0..1)
```

---

## ۳. نمونهٔ گراف (پس از بارگذاری کورپوس پایه + یک آیین‌نامهٔ ورزشی)

```cypher
// قانون پایه و آیین‌نامهٔ مجری آن
(:Authority {name:'مجلس شورای اسلامی'})
      -[:ISSUES]-> (:LegalInstrument {title:'قانون تدوین و تنقیح…', tier:3})
(:LegalInstrument {title:'آیین‌نامه تدوین و تنقیح مقررات', tier:4})
      -[:IMPLEMENTS {authority_basis:'مادهٔ ۲', review_status:'expert_approved'}]->
       (:LegalInstrument {title:'قانون تدوین و تنقیح…', tier:3})
(:LegalInstrument {title:'قانون اهداف، وظایف و اختیارات وزارت ورزش', tier:3})
      <-[:IMPLEMENTS]- (:LegalInstrument {title:'آیین‌نامه اجرایی …', tier:4})
// سیر نسخ
(:DocumentVersion {version_label:'مصوب ۱۳۵۸'})
      <-[:HAS_VERSION]- (:LegalInstrument {title:'قانون اساسی', tier:1})
(:DocumentVersion {version_label:'اصلاحی ۱۳۶۸'}) -[:AMENDS {mode:'اصلاح کلی'}]->
      (:LegalInstrument {title:'قانون اساسی', tier:1})
// ناسخ صریح با شاهد
(:LegalInstrument {title:'قانون الف'}) -[:REPEALS {mode:'explicit', scope:'partial',
      evidence_span:'مادهٔ ۷', review_status:'expert_approved'}]-> (:Provision {number:'۵'})
```

## ۴. اندیس‌ها (FalkorDB)

تطابق `adapters/falkordb.py` (متد `ensure_index`):

```cypher
CREATE INDEX FOR (i:LegalInstrument) ON (i.canonical_title)
CREATE INDEX FOR (i:LegalInstrument) ON (i.tier)
CREATE INDEX FOR (i:LegalInstrument) ON (i.external_id)
CREATE INDEX FOR (p:Provision) ON (p.provision_id)
CREATE INDEX FOR (pv:ProvisionVersion) ON (pv.normalized_text)   -- جستجوی واژگانی
CREATE INDEX FOR (sd:SourceDocument) ON (sd.checksum)
```

کلید هر سازمان (per-org graph key) مطابق رفتار فعلی آداپتور حفظ می‌شود.

## ۵. مپینگ به مدل کنونیکال (بدون شکستگی)

| مفهوم این طرح | پیاده‌سازی موجود |
|---|---|
| LegalInstrument (+tier) | `canonical.LegalInstrument` — `tier` → فیلد `authority_level` |
| DocumentVersion | `canonical.DocumentVersion` (enacted_at در V0005 هم ذخیره می‌شود) |
| Provision/ProvisionVersion | `canonical.Provision/ProvisionVersion` |
| SourceSpan / ExplicitReference | `canonical.SourceSpan/ExplicitReference` |
| یال‌های ۲٫۲ و ۲٫۳ | `CanonicalEdgeType.IMPLEMENTS/EXPLICITLY_REFERENCES/AMENDS/REPEALS/SUSPENDS/RESTORES/REPLACES/SUPERSEDES` |
| provenance / review_status | `canonical.Provenance` + `ProgressiveStatus` |
| `mode` نسخ (explicit/implicit) | `AmendmentRelationType` + property روی یال (نیاز به یک فیلد در `CanonicalEdge.metadata`) |
| قانون خاص (لایه ۲) | افزودن `InstrumentType.SPECIAL_STATUTE` و `CABINET_APPROVAL` به enum |
| رویداد تغییر حقوقی (LegalEffect) | **نود جدا نمی‌سازیم** — همان یال‌های AMENDS/REPEALS/…؛ صف و شاهدِ آن‌ها در جدول `sources.legal_effects` (V0005) زندگی می‌کند و فقط approvedها به یال منتشر می‌شوند |
| SourceRecord (هر نسخه منبع) | `(:SourceDocument)` + ردیف `sources.source_documents`؛ چند رکورد منبع از یک قانون از طریق `instrument_uid` به یک `(:LegalInstrument)` می‌رسند |

نتیجه: بارگذاری کورپوس پایه فقط «پر کردن» همین ساختار است؛ هیچ تغییر شکست‌خورده‌ای در مدل لازم نیست.

قاعدهٔ جهت‌دار یال‌ها: هر رابطه فقط یک جهت ذخیره می‌شود (`REPEALS`)؛ خواندن معکوس در پرس‌وجو انجام می‌شود — ذخیرهٔ هم‌زمان `REPEALS` و `REPEALED_BY` ممنوع (خطر ناسازگاری).
