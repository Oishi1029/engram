FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engram/ ./engram/

# Cloud Run injects PORT. One worker: this service runs an LLM agent whose concurrency is
# bounded deliberately, and extra workers would multiply billed model calls, not throughput.
ENV PORT=8080
CMD exec uvicorn engram.server:app --host 0.0.0.0 --port ${PORT} --workers 1
