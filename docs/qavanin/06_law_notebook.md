# ۰۶ — درخت نمایشی یگانه و دفترچهٔ پژوهش (Law Notebook)

> ایده: پیمایش قوانین مثل پیمایش یک کدبیس — قانون=فایل، ماده=تابع، ارجاع صریح=function call،
> و یک «دفترچه» که تجربهٔ مسیرهای پژوهش را نگه می‌دارد. این سند تصمیم معماری نهایی را ثبت می‌کند
> و مرز «نمایش» را از «ادعای حقوقی» جدا می‌سازد.

## ۰. تصمیم‌های قطعی

1. **درخت یگانهٔ نمایشی می‌سازیم، اما به‌عنوان projection.** هر سند یک «جای اصلی نمایش» دارد؛
   یال درخت هرگز ادعای حقوقی نیست. این همان مدل کدبیس است: پوشه=view، import=حقیقت.
2. **گراف، حقیقت می‌ماند.** روابط حقوقی تایپ‌دار و با provenance در `canonical.graph_edges`
   زندگی می‌کنند و معنایشان با ستون `claim` از هم تفکیک می‌شود (بخش ۲).
3. **کلسن فقط تا مرز صلاحیت.** سلسلهمراتب هنجارها (Stufenbau) دربارهٔ *اعتبار/صلاحیتِ ایجاد*
   است، نه زیرموضوعی محتوا (SEP، Pure Theory of Law). پس «عمق درخت = موضوع + اعتبار»
   به‌عنوان *ادعا* رد شد؛ فقط به‌عنوان *قرارداد نمایش* پذیرفته شد.
4. **دستهبندی منابع:** قانون+ماده و رأی وحدت رویه (لازم‌الاتباع، مادهٔ ۴۷۱ ق.آ.ک) = منبع اصلی؛
   نظریهٔ مشورتی/آرای عادی = لایهٔ مستند (annotation)؛ رأی ابطال دیوان عدالت = تغییردهندهٔ اعتبار
   با اثر زمانی (مادهٔ ۱۳ ق.دیوان) — هیچ‌کدام «کامنت» نیستند و هویت مستقل دارند.
5. **دفترچه رویداد-محور + خلاصهٔ ادغام‌شده، کاندید+بازبینی**؛ وزن مسیر از *نتیجهٔ تأییدشده*
   تغذیه می‌شود نه از صرفِ بازکردن سند؛ دفترچهٔ موضوع ≠ دفترچهٔ سند؛ کلید موضوع پایدار است.

## ۱. نگاشت مفهوم به کدبیس

| حقوق | نرم‌افزار | موجود در ریپو |
|---|---|---|
| قانون اساسی | مشخصات زبان / قواعد بنیادی | tier 1 |
| قانون | interface (تعهد کلی) | tier 2/3 `legal_instruments` |
| آیین‌نامه/تصویب‌نامه | کلاس concrete پیاده‌کننده | tier 4 + یال `implements` |
| بخشنامه/دستورالعمل | helper / config | tier 5 |
| ماده / تبصره/بند | تابع / شرط درون تابع | `provisions` + parent links |
| ارجاع صریح «موضوع مادهٔ ۵ قانون X» | function call | `explicitly_references` (۱۵٬۵۳۱) |
| رأی وحدت رویه | تست الزام‌آور / پچ رسمی | `Judgment` + CITES |
| نظریهٔ مشورتی | docstring/کامنت | `AdvisoryOpinion` |
| نسخ/اصلاح | deprecate/refactor | `amends`/`repeals` |
| دفترچهٔ پژوهش | حافظهٔ مهندس (notebook) | **جدید** (بخش ۶–۸) |

## ۲. سه‌گانهٔ روابط — که یکی نمی‌شوند

| رابطه | نمونه | معنا | یال |
|---|---|---|---|
| جزءبودن | ماده داخل فصل | ساختار واقعی متن | `CONTAINS` (parent_provision_id) |
| مبنای وضع/اجرا | آیین‌نامهٔ اجرایی مادهٔ ۴ | وابستگی حقوقی مستند | `implements` (+`claim='statutory_basis'`) |
| ارتباط موضوعی | چند قانون دربارهٔ خانواده | دسته‌بندی پژوهشی | `subject_domain` / `related_to` |

ستون جدید `claim` روی `canonical.graph_edges` (V0011) معنای هر یال را صریح می‌کند:

| claim | معنا | edge_typeهای فعلی |
|---|---|---|
| `structure` | جزءبودن متنی | — (در provisions) |
| `statutory_basis` | مبنای وضع/اجرا (مستند یا استنباطی) | `implements` |
| `citation` | ارجاع/استناد متنی | `explicitly_references`, `cites` |
| `amendment` | تغییر وضعیت حقوقی | `amends`, `repeals` |
| `site_related` | پیوند منتخب گردانندهٔ سایت | `related_to` |
| `display_hint` | حدس سازمان‌دهی (نه حقوقی) | `subordinate_to` (created_by='domain-hierarchy', conf=0.5) |

قاعده: **هیچ مصرف‌کننده‌ای (agent/UI) حق ندارد یال `display_hint` را به‌عنوان رابطهٔ حقوقی
نقل کند.** ابزارها باید claim را برگردانند.

## ۳. لایهٔ قضایی — تفکیک دقیق

| نوع | الزام | اثر زمانی | مدل |
|---|---|---|---|
| رأی وحدت رویه | لازم‌الاتباع (مادهٔ ۴۷۱ ق.آ.ک) | از تاریخ انتشار | `Judgment` با kind=`unity_of_precedent` |
| رأی ابطال هیئت عمومی دیوان | وضعیت اعتبار مقرره را تغییر می‌دهد | قاعده: از صدور؛ استثنا: تسری به گذشته (مادهٔ ۱۳) | یال `annuls` + scope + effective_from |
| نظریهٔ مشورتی | غیرالزام‌آور، تفسیری | — | `AdvisoryOpinion` (annotation) |
| رأی عادی | فقط طرفین دعوا | — | `Judgment` عادی (annotation) |

هیچ‌کدام در درخت نمایشی «پوشه» نمی‌گیرند؛ به‌صورت نود مستقل با یال `CITES/INTERPRETS/ANNULS`
به مواد وصل می‌شوند و هویت/منبع/تاریخ مستقل خود را حفظ می‌کنند.

## ۴. درخت نمایشی یگانه

### قاعدهٔ جایگذاری (یک تابع، دترمینیستیک)

```
place(norm):
    P = والد تجسیدی مشخص          # implements با claim='statutory_basis'،
                                   # به‌جز یال‌های «به قانون اساسی» (fallback ضعیف)
    if P:  return child(P), basis='derivation'
    else:  return child(موضوع), basis='topic'
```

گاردها:
- یال implementsِ مقصدش قانون اساسی → نادیده (فقط اعتبار عمومی، نه والد ساختاری). (۱۹۴ مورد فعلی)
- حلقه/دووالد → اولویت با confidence بالاتر؛ log می‌شود.
- سند بدون domain و بدون والد → پوشهٔ «طبقه‌بندی‌نشده».

### شکل درخت

```
root (قانون اساسی + کلیات)
└─ L1 شاخه‌های کلان (گروه‌بندی ۳۳ subject_domain — جدول نگاشت ثابت)
   └─ L2 موضوع (subject_domain)
      └─ L3+ سند → (ساختار درونی: کتاب/باب/فصل) → مواد
```

هر یال درخت یک property حمل می‌کند: `basis: derivation|topic`، `edge_id?` (اگر از یال
گراف آمده)، `confidence`. مثال واقعی:

```
root / حقوق مدنی و خانواده / خانواده
├─ قانون حمایت از خانواده و جوانی جمعیت            (basis=topic)
│  ├─ آیین‌نامه اجرایی مادهٔ ۴ …                     (basis=derivation, edge:impl:…)
│  ├─ دستورالعمل اجرایی مادهٔ ۶۵ …                   (basis=derivation)
│  └─ آیین‌نامه اعطای واحدهای سرمایه‌گذاری…          (basis=derivation)
└─ مدنی / قانون مدنی → [کتاب نکاح → باب مهریه] → مادهٔ ۱۰۸۲
```

رأی/نظریه اینجا نیستند؛ از گرهٔ ماده با `get_related` می‌آیند (میانبر، نه جای اصلی).

## ۵. ساختار درونی اسناد (پیش‌نیاز ناوبری درون کدها)

وضعیت: مواد قانون مدنی (۱۵۳۳ ماده) همه root ذخیره شده‌اند — کتاب/باب/فصل استخراج نشده.
کار: اسکریپت دترمینیستیک که از سرفصل‌های متن (الگوهای «کتاب …»، «باب …»، «فصل …»، «مبحث …»)
کانتینر می‌سازد و parentهای موجود را با همان قواعد `ingestion/hierarchy.py` بازچینی می‌کند.
(فاز ۲ — بدون LLM؛ خروجی قابل بازبینی در report.)

## ۶. مدل دادهٔ دفترچه

Schema جدید `notebook` (V0012)، RLS سازمان‌محور مثل `research_history`:

- `notebook.topics` — کلید پایدار موضوع: `topic_id`, `name`, `parent_topic_id` (درخت موضوعی پایدار، مستقل از نام فارسی پوشه).
- `notebook.events` — append-only: `event_id, organization_id, episode_id, topic_id?, node_id?, event_type, payload jsonb, created_at`. انواع: `route_taken`, `relation_discovered`, `answer_confirmed`, `summary_merged`.
- `notebook.routes` — `(org, from_topic_id, topic_label)` → `to_node_id`, `weight`, `hits`, `confirmed_hits`, `status(candidate|approved)`, `source_episode_id`.
- `notebook.pages` — خلاصهٔ ادغام‌شده هر گره/موضوع: `summary`, `summary_version`, `based_on jsonb` (نسخه‌ها/چکسام‌ها)، `needs_revalidation bool`, `updated_at`.

روابط کشف‌شدهٔ ماهوی **تکرار نمی‌شوند** — همان `ProgressiveRelation` گراف research هستند
(candidate→expert_approved)؛ صفحهٔ دفترچه فقط آنها را گروه‌بندی و نمایش می‌دهد.

## ۷. مسیر نوشتن (پس از هر اپیزود)

هوک در پایان اپیزود (`agentic/loop.py`):
- مسیر طی‌شده → `route_taken` (وزن اولیه کم).
- **فقط وقتی پاسخ verify شد و شواهدش accept شد** → `answer_confirmed` → وزن/confirmed_hits مسیرهای همان اپیزود تقویت می‌شود. بازکردن سند به‌تنهایی هیچ وزنی نمی‌دهد.
- رابطهٔ ضمنیِ تازه (نه در graph_edges) → `ProgressiveRelation` با status=candidate.
- همه‌چیز کاندید است؛ بازبینی با endpointهای موجود progressive-memory.

## ۸. مسیر خواندن (ابزارهای agent)

- `list_tree(node_id?)` — فرزندان یک گرهٔ درخت (شاخه/موضوع/سند) با خلاصه و برچسب `basis`؛ پیمایش پوشه‌به‌پوشه (مثل `cd`).
- `walk_tree(topic)` — مسیر(های) ریشه→…→سند برای یک موضوع، با دلیل هر پرش (`derivation|topic`).
- `notebook_index(domain?)` — نقشهٔ موضوعات + خلاصهٔ دفترچهٔ هرکدام.
- `notebook_route(topic)` — مسیرهای پژوهش تأییدشده (کوتاه‌کنندهٔ ناوبری).
- `open_notebook(node|topic)` — خلاصهٔ زیردرخت + روابط approved + مسیرها.
- skill جدید `skills/notebook_navigation.md` + router keywords.
قاعدهٔ طلایی: درخت/دفترچه فقط **کوتاه‌کنندهٔ مسیر** است؛ برای پاسخ نهایی، متن و نسخهٔ معتبر
دوباره از canonical خوانده می‌شود (هرگز cite نمی‌شود).

### دلیل تایپ‌دار هر گام
هر فراخوانی ابزار در trace یک `reason` می‌گیرد: `structure | citation | statutory_basis |
precedent | version | search | open | read | notebook | graph | other`. این‌گونه مسیر
agent به‌صورت «استدلال‌شده» خوانده می‌شود، نه فقط فهرست ابزارها.

## ۹. کار ادغام خلاصه‌ها (آنالوگ notes_commit)

`scripts/merge_notebook_pages.py` — آفلاین، incremental (فقط گره‌های کثیف):
ورودی: events + روابط approved + خلاصه‌های canonical فرزندان → بازنویسی فشردهٔ `pages.summary`
(فارسی). `based_on` پر می‌شود؛ اگر سندِ ارجاع‌شده اصلاح/منسوخ شد → `needs_revalidation=true`
(همان badge freshness دفترچه‌ها).

## ۱۰. فازبندی اجرا

| فاز | محتوا | وضعیت |
|---|---|---|
| ۰ | همین سند | ✅ |
| ۱ | V0011: `claim`+`review_status` روی graph_edges + بکفیل + mirror/sync | ✅ |
| ۲ | استخراج ساختار درونی (کتاب/باب/فصل) | ✅ (۱۸۹ سند) |
| ۳ | `build_display_tree.py` (projection با basis) | ✅ |
| ۴ | دفترچه: V0012 + models + repo + tools + seed + capture + merge | ✅ |
| ۵ | ارزیابی با بنچمارک‌های موجود (social_security/sports) | ⏳ |

## ۱۲. دروازه‌های پذیرش (Acceptance gates)

فایل `tests/test_navigation_goal.py` «سنجش هدف» است: هر وقت همهٔ این‌ها پاس شدند، یعنی
ناوبری به هدف «شبیه وکیل/برنامه‌نویس» رسیده است.

| دروازه | ادعا |
|---|---|
| TreeNavigationGate | درخت سطح‌به‌سطح پیمایش می‌شود؛ `walk_tree` مسیر برچسب‌دار تا سند می‌دهد؛ اسناد `derivation` زیر قانون مادر nest شده‌اند |
| InternalStructureGate | مادهٔ ۱۰۸۲ قانون مدنی اجداد کتاب/باب/فصل دارد (عمق ≥ ۳) — دیگر تخت نیست |
| ClaimSeparationGate | هر یال `claim`+`review_status` دارد؛ `subordinate_to`=`display_hint` و `implements`=`statutory_basis`؛ یال‌های ارجاعی حذف نشده‌اند (≥ ۲۱۰۰۰) |
| NotebookGate | مسیر تأییدشده round-trip می‌شود؛ روابط ضمنیِ هم‌مصرف به کاندید `commonly_check_with` تبدیل می‌شوند (id دترمینیستیک) |
| TypedReasonGate | ابزارهای کلیدی reason تایپ‌دار دارند |
| CompositeSurfaceGate | سطح واحد agent هر سه لایه (درخت/دفترچه/گراف) را تبلیغ و مسیریابی می‌کند |

اجرای دروازه‌ها: `.venv/bin/python -m unittest tests.test_navigation_goal -v`
(دروازه‌های DB-محور در نبود Postgres به‌صورت skip اجرا می‌شوند).

### اتصال‌های انجام‌شدهٔ فاز ۴
- `NotebookTools` در `CompositeTools` ثبت شد و به کاتالوگ پرامپت پلنر اضافه می‌شود.
- `_capture_notebook` در `ResearchApplicationService` (هم `run` و هم `stream_events`): رویدادها همیشه ثبت می‌شوند؛ **وزن مسیر فقط با پاسخ تأییدشده** (نه صرف open).
- `_notebook_topic_resolver` در `composition` از `out/display_tree.json` سند را به موضوع دامنه نگاشت می‌کند.
- `scripts/merge_notebook_pages.py` (check/rebuild + freshness) اجرا شد.

## ۱۱. ریسک‌ها

- نویز کاندیدها → آستانهٔ وزن/تکرار قبل از نمایش مسیر.
- یال‌های implements با «به استناد» ممکن است مبنای صلاحیت باشند نه اجرا → claim فعلاً
  `statutory_basis` واحد می‌ماند؛ تفکیک دقیق `execution`/`competence_basis` نیازمند بازبینی
  شواهد است (کار آینده با review UI).
- هزینهٔ ادغام LLM برای هزاران گره → incremental + فقط کثیف‌ها.
- ۲۹ سند بدون domain و ۱۲۸ «سایر» → پوشهٔ «طبقه‌بندی‌نشده» تا بازطبقه‌بندی.
