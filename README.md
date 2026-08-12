# Legal Agent Core

اولین برش اجرایی ADR-001، ADR-002 و ADR-003 برای دستیار پژوهش حقوقی زمان‌مند.

## آنچه پیاده‌سازی شده است

- مدل canonical برای `SourceDocument`، `LegalInstrument`، `DocumentVersion`، `Provision`، `ProvisionVersion`، `SourceSpan` و `Citation`
- هویت پایدار Provision در کنار نسخه‌های تاریخی و فیلتر `applicable_time`
- حفظ جداگانه‌ی متن خام و normalized و ارجاع دقیق به نسخه/صفحه/span
- مرز ذخیره‌سازی `CanonicalRepository` و `ResearchGraphRepository`، مستقل از FalkorDB
- relationهای canonical ساختاری/حقوقی با provenance و الزام SourceSpan برای ارجاع صریح
- overlay مستقل هر سازمان برای Progressive Research Graph
- workflow اجباری `candidate -> expert_approved`
- ثبت audit event برای تغییرات حافظه‌ی پژوهشی
- انتقال خودکار دانش تأییدشده به `needs_revalidation` هنگام منسوخ شدن نسخه‌ی منبع
- migration نسخه‌دار PostgreSQL برای schemaهای مجزای `canonical` و `research`
- migration runner سازگار با connectionهای PEP-249/psycopg با تشخیص checksum drift
- adapter رسمی FalkorDB پشت `ResearchGraphRepository` با queryهای پارامتری و optimistic writes
- ingestion pipeline مستقل از parser با شناسه‌های deterministic و اجرای idempotent
- استخراج محافظه‌کارانه‌ی ارجاع صریح فارسی و resolve فقط با scope مشخص
- Evidence Ledger و Verifier با کنترل identity، متن منبع، زمان، تناقض و referenceهای دنبال‌نشده
- Navigation Loop مستقل از Retriever و LLM با توقف موفق، ادامه‌ی تحقیق و تشخیص stall
- Multi-signal Retriever با نرمال‌سازی فارسی، temporal/scope gate و ترکیب سیگنال‌های متن، metadata و graph
- LLM Gateway برای Responses و Chat Completions با profileهای مستقل، prompt versioning و retry/timeout
- Answer Composer ساختاریافته و Citation Pipeline با منع انتشار Claim بدون Evidence تأییدشده
- PostgreSQL CanonicalRepository و Unit of Work با queryهای پارامتری و writeهای idempotent
- صف ingestion با `SKIP LOCKED`، retry/dead-letter، stale-lock fencing و transaction اتمیک
- adapterهای OCR/parser و workflowهای correction annotation و amendment linking
- تست invariantهای اصلی ADRها

## ساختار

```text
src/legal_agent_core/
  canonical.py      # Canonical Document IR
  research.py       # Research episodes, evidence, learned relations
  repositories.py   # Storage ports
  in_memory.py      # Strict reference adapters
  retrieval.py      # Explainable temporal and graph-aware retrieval
  verification.py   # Evidence ledger and deterministic verifier
  navigation.py     # Iterative research loop
  llm.py            # Model profiles, prompt registry and LLM service
  answering.py      # Structured composition and verified citation publishing
  services.py       # Approval and revalidation policy
  adapters/
    falkordb.py      # Tenant-isolated progressive graph adapter
    llm_http.py      # OpenAI-compatible HTTP gateway
    postgres.py      # Canonical PostgreSQL repository and unit of work
    postgres_ingestion.py # Durable ingestion repository and worker
  ingestion/
    models.py        # Parser-neutral ingestion contract
    operations.py    # Jobs, OCR/parser ports, corrections and amendments
    pipeline.py      # Deterministic canonical graph build
    references.py    # Persian explicit-reference extraction
  verification.py   # Claim-to-evidence and temporal verification
  navigation.py     # Bounded Find/Read/Verify loop
  db/
    migrator.py     # Versioned PostgreSQL migration runner
    migrations/     # Canonical IR and research event-log schema
tests/
```

## اجرای تست‌ها

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

پروژه در این مرحله هیچ dependency اجرایی یا توسعه‌ای ندارد و suite با کتابخانه‌ی استاندارد Python اجرا می‌شود.

برای lint قابل‌بازتولید:

```powershell
python -m pip install -e ".[dev]"
python -m ruff check src tests
```

## اجرای migration پایگاه داده

Runner یک connection سازگار با PEP-249 دریافت می‌کند؛ برای نمونه با `psycopg`:

```python
import psycopg
from legal_agent_core.db import apply_migrations

with psycopg.connect("postgresql://localhost/legal_agent") as connection:
    apply_migrations(connection)
```

فایل SQL مستقیماً نیز در `src/legal_agent_core/db/migrations` قابل اجرا است. Runner نسخه و checksum هر migration را در جدول `public.legal_agent_schema_migrations` ثبت می‌کند.

جدول‌های schema پژوهشی Row-Level Security دارند. هر transaction عملیاتی باید tenant را پیش از query مشخص کند:

```sql
SET LOCAL legal_agent.organization_id = 'org-a';
```

در صورت تنظیم‌نشدن tenant، policy به‌صورت پیش‌فرض هیچ ردیف سازمانی را قابل مشاهده یا نوشتن نمی‌کند.

## FalkorDB adapter

نصب dependency اختیاری:

```powershell
python -m pip install -e ".[falkordb]"
```

ساخت adapter و indexهای هر سازمان:

```python
from legal_agent_core.adapters import FalkorResearchGraphRepository
from legal_agent_core.services import ProgressiveMemoryService

repository = FalkorResearchGraphRepository.connect(
    host="localhost",
    port=6379,
    password=None,
)
repository.ensure_indexes("org-a")
memory = ProgressiveMemoryService(repository)
```

برای هر سازمان یک graph key مستقل و مشتق‌شده از SHA-256 ساخته می‌شود. علاوه بر این جداسازی فیزیکی، تمام Nodeها و queryها `organization_id` دارند. Write مربوط به تغییر relation و ایجاد `MemoryEvent` در یک Cypher query انجام می‌شود و transition وضعیت با optimistic concurrency کنترل می‌شود.

Readها از `GRAPH.RO_QUERY` و تمام ورودی‌ها از پارامترهای Cypher استفاده می‌کنند؛ شناسه یا متن کاربر در query interpolate نمی‌شود.

### تست integration واقعی FalkorDB

با یک server در دسترس:

```powershell
$env:FALKORDB_URL = "falkor://localhost:6379"
.\.venv\Scripts\python.exe -m unittest tests.integration.test_falkordb_live -v
```

در Linux/macOS با Python 3.12+ می‌توان embedded runtime رسمی را نصب کرد؛ test در نبود URL آن را خودکار پیدا می‌کند:

```bash
python -m pip install -e '.[falkordb,falkordb-lite]'
python -m unittest tests.integration.test_falkordb_live -v
```

`FalkorDBLite` روی Windows native توسط dependency آن پشتیبانی نمی‌شود؛ در Windows باید server از طریق Docker، WSL یا یک URL خارجی فراهم شود.

## Canonical ingestion

Pipeline هیچ وابستگی به OCR یا parser خاص ندارد. هر provider باید خروجی خود را به `ParsedDocument` تبدیل کند:

```python
from legal_agent_core.ingestion import CanonicalIngestionPipeline

result = CanonicalIngestionPipeline(canonical_repository).ingest(parsed_document)
```

شناسه‌های Source، Instrument، DocumentVersion، Provision، ProvisionVersion و SourceSpan از identityهای حقوقی و محتوای normalized به‌صورت deterministic ساخته می‌شوند. اجرای مجدد همان خروجی parser داده‌ی قبلی را overwrite نمی‌کند و نتیجه‌ی یکسان می‌دهد.

استخراج‌کننده‌ی اولیه عبارت‌هایی مانند «ماده ۴ این قانون» یا «ماده ۴ قانون نمونه» را resolve می‌کند. عبارت بدون scope مانند «ماده ۴» ثبت می‌شود اما `unresolved` باقی می‌ماند؛ بنابراین شباهت یا حدس، canonical identity تولید نمی‌کند.

## PostgreSQL persistence و ingestion operations

نصب adapter:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[postgres]"
```

`PostgresCanonicalRepository` تمام read/writeهای `CanonicalRepository` را پیاده‌سازی می‌کند. writeها پارامتری و idempotent هستند و استفاده از یک identity برای محتوای متفاوت خطا می‌دهد. `PostgresUnitOfWork` در موفقیت commit و در خطا rollback می‌کند.

Migration دوم schema عملیاتی ingestion را اضافه می‌کند:

- jobهای idempotent با claim اتمیک `FOR UPDATE SKIP LOCKED`
- exponential retry، dead-letter و fencing بر اساس worker/attempt
- correction annotation بدون overwrite کردن متن canonical
- amendment/repeal/replace/suspend/restore link بین DocumentVersionها
- قراردادهای مستقل `SourceLoader`، `DocumentParser` و `OCRAdapter`

تست واقعی PostgreSQL به DSN نیاز دارد:

```powershell
$env:LEGAL_AGENT_POSTGRES_DSN = "postgresql://user:password@localhost/legal_agent"
.\.venv\Scripts\python.exe -m unittest tests.integration.test_postgres_live -v
```

## Evidence verification و navigation

`EvidenceVerifier` هر Claim مهم را به Evidence Ledger متصل می‌کند و موارد زیر را کنترل می‌کند:

- تطبیق کامل `DocumentVersion / ProvisionVersion / SourceSpan / page`
- وجود excerpt در متن canonical
- اعتبار نسخه در `applicable_time`
- statusهای draft، suspended و unknown
- overlap نسخه‌های زمانی
- Evidence متناقض
- reference صریح resolved که هنوز دنبال نشده است

`NavigationLoop` به Retriever و AnswerComposer مشخصی وابسته نیست. اگر Verification رد شود، issueهای ساختاریافته را در iteration بعد به Retriever می‌دهد؛ Evidence نامعتبر از Draft بعدی حذف می‌شود و loop در موفقیت، رسیدن به سقف iteration یا stall متوقف می‌شود.

## Multi-signal retrieval

`MultiSignalEvidenceRetriever` ابتدا نسخه‌های ناسازگار با تاریخ سؤال یا document scope را حذف می‌کند. سپس امتیازهای lexical، metadata، ارجاع صریح resolved و Progressive Memory تأییدشده را ترکیب می‌کند. خروجی شامل دلیل انتخاب و امتیاز هر سیگنال است و ارجاع‌های دنبال‌شده نیز در trace پژوهش ثبت می‌شوند.

```python
from legal_agent_core.retrieval import MultiSignalEvidenceRetriever

retriever = MultiSignalEvidenceRetriever(
    canonical_repository, research_graph_repository
)
```

## LLM Gateway و Model Profiles

Gateway به SDK خاصی وابسته نیست و دو قرارداد `Responses` و `Chat Completions` را پشتیبانی می‌کند؛ بنابراین همان لایه برای provider ابری یا endpoint محلی OpenAI-compatible قابل استفاده است. profileهای `navigation`، `extraction`، `verification` و `answer_composition` مستقل و نسخه‌دار هستند. API key در repr یا خطاها نمایش داده نمی‌شود و `store=False` پیش‌فرض درخواست‌های Responses است.

```python
from legal_agent_core.adapters import OpenAICompatibleGateway
from legal_agent_core.llm import (
    EndpointStyle,
    LLMService,
    ModelProfileRegistry,
    PromptRegistry,
    ProviderConfig,
    legal_model_profiles,
    legal_prompt_templates,
)

provider = ProviderConfig(
    "primary",
    "https://api.openai.com/v1",
    EndpointStyle.RESPONSES,
    api_key="...",
)
profiles = ModelProfileRegistry()
for item in legal_model_profiles("primary", "configured-model"):
    profiles.register(item, activate=True)
service = LLMService(
    OpenAICompatibleGateway(),
    {"primary": provider},
    profiles,
    PromptRegistry(*legal_prompt_templates()),
)
```

تست زنده عمداً opt-in است تا بدون اجازه هزینه ایجاد نکند:

```powershell
$env:LEGAL_AGENT_RUN_LIVE_LLM = "1"
$env:LEGAL_AGENT_LLM_BASE_URL = "https://api.openai.com/v1"
$env:LEGAL_AGENT_LLM_MODEL = "your-configured-model"
$env:LEGAL_AGENT_LLM_API_KEY = "..."
.\.venv\Scripts\python.exe -m unittest tests.integration.test_llm_live -v
```

## Answer و Citation Pipeline

`LLMAnswerComposer` خروجی مدل را با JSON Schema به `DraftAnswer` و Claimهای صریح تبدیل می‌کند. شناسه‌های Evidence توسط مدل تولید یا تفسیر نمی‌شوند؛ هر Claim فقط شناسهٔ Evidenceهای موجود را اعلام می‌کند و `EvidenceVerifier` آن‌ها را به منبع canonical و تاریخ سؤال متصل می‌کند.

`CitationPipeline` تنها زمانی پاسخ را منتشر می‌کند که گزارش راستی‌آزمایی به همان `answer_id` و `applicable_time` تعلق داشته باشد و تمام Evidenceهای هر Claim در Ledger پذیرفته شده باشند. خروجی شامل citation دقیق نسخه/ماده/span/صفحه، claim-to-evidence map و audit کامل model/profile/prompt/agent است.

```python
from legal_agent_core.answering import CitationPipeline, LLMAnswerComposer

composer = LLMAnswerComposer(llm_service)
published = CitationPipeline(canonical_repository).publish(
    outcome.draft,
    outcome.verification,
    outcome.ledger,
    applicable_time,
)
```

## REST API، امنیت و ارزیابی

گام پایانی Backend MVP یک API مبتنی بر FastAPI اضافه می‌کند. همه‌ی endpointهای دامنه به‌جز health check به Bearer authentication نیاز دارند و هر مجوز علاوه بر نقش، مرز `organization_id` را نیز کنترل می‌کند. `StaticTokenVerifier` فقط برای تست و محیط محلی است؛ محیط production باید یک پیاده‌سازی `TokenVerifier` متصل به identity provider خود تزریق کند.

نصب وابستگی‌های API و تست:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[api,dev,postgres]"
```

endpointهای اصلی:

- `POST /v1/research`
- `GET /v1/organizations/{organization_id}/research/{episode_id}`
- `POST /v1/organizations/{organization_id}/relations/{relation_id}/approve`
- `POST /v1/organizations/{organization_id}/relations/{relation_id}/reject`
- `POST /v1/organizations/{organization_id}/evaluations`
- `GET /v1/organizations/{organization_id}/metrics`
- `GET /healthz`

`create_app(APIContainer(...))` composition root برنامه را می‌سازد. سرویس کاربردی API همان Navigation/Verification/Citation pipeline را اجرا می‌کند؛ بنابراین پاسخ منتشرشده از publish gate میان‌بُر نمی‌زند. ورودی‌ها strict هستند، request ID روی پاسخ برگردانده می‌شود، خطاهای domain به HTTP status مناسب نگاشت می‌شوند، و metricها بدون labelهای پرحجم و به تفکیک tenant ارائه می‌شوند.

Evaluation harness نرخ تکمیل، citation precision/recall، temporal accuracy و grounded-claim rate را برای مجموعه test case محاسبه می‌کند.

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

جزئیات پیشرفت UI و کارهای production در [ROADMAP.md](ROADMAP.md) نگهداری می‌شود.

## Research Workspace

رابط فارسی در `/workspace` مستقیماً از همان FastAPI سرو می‌شود و به build tool یا CDN خارجی وابسته نیست. قابلیت‌های فعلی شامل ثبت سؤال و تاریخ قابل اعمال، پاسخ مستند، کارت‌های citation، Research Trace، Evidence Ledger و صف بازبینی کارشناس است. داده‌های خروجی با DOM API و `textContent` نمایش داده می‌شوند و صفحه دارای CSP و security headerهای محدودکننده است.

اجرای نسخه نمایشی محلی:

```powershell
.\.venv\Scripts\python.exe -m uvicorn legal_agent_core.demo:app --host 127.0.0.1 --port 8000
```

سپس `http://127.0.0.1:8000/workspace` را باز کنید. داده‌های نسخه نمایشی:

- سازمان: `org-a`
- توکن پژوهشگر: `researcher-demo`
- توکن کارشناس با دسترسی صف بازبینی: `expert-demo`
- سؤال نمونه: `مهلت تجدیدنظر برای اشخاص مقیم ایران چند روز است؟`

توکن‌های demo عمومی‌اند و فقط برای composition root حافظه‌ای `legal_agent_core.demo` تعریف شده‌اند؛ استفاده از آن در محیط production ممنوع است. برنامه واقعی باید `APIContainer` را با repositoryهای PostgreSQL/FalkorDB، LLM gateway و `TokenVerifier` متصل به identity provider بسازد.
