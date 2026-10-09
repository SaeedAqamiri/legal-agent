# legal-agent

پلتفرم پژوهش حقوقی زمان‌مند (temporal legal research) — مبتنی بر ADR.

سه لایه:

| لایه | نگهدارنده | نقش |
|---|---|---|
| انبار محتوا | Postgres (`canonical` + `sources`) | اصل PDF/متن قوانین + میتاداتا — «منبع حقیقتِ محتوا» |
| گراف دانش | FalkorDB (با fallback در-memory) | سلسله‌مراتب، نسخ/ناسخ، ارجاع‌ها، اورلی پژوهش پیش‌رونده |
| خط تولید | کانکتورها + پارسر + LLM + بازبینی انسان | تبدیل ۱→۲ به‌صورت idempotent و قابل ردیابی |

## اجرا

```bash
docker compose up -d --build   # Postgres + FalkorDB + API
# API: http://127.0.0.1:8000  (UI: /workspace)
```

بدون docker (حالت سبک، in-memory):

```bash
pip install -e ".[api,postgres,oidc]"
LEGAL_AGENT_POSTGRES_DSN=postgresql://... python -m uvicorn legal_agent_core.composition:app --port 8000
```

## اینجست کورپوس قوانین

```bash
LAWS_DATA_DIR=~/laws-mcp/data LEGAL_AGENT_POSTGRES_DSN=... \
    .venv/bin/python scripts/ingest_laws_data.py
```

مستندات معماری: `docs/qavanin/`

## توسعه

```bash
python -m unittest discover -s tests -p "test_*.py"
ruff check src scripts tests
```
