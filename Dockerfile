FROM python:3.14-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api,postgres,oidc,dev,falkordb]"

# Runtime image
FROM base AS runtime
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "legal_agent_core.composition:app", "--host", "0.0.0.0", "--port", "8000"]
