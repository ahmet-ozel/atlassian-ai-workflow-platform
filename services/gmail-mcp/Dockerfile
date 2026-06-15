# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

RUN python -m pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir fastapi "uvicorn[standard]" \
    && useradd -u 10001 -m appuser

WORKDIR /app

COPY src/ ./src/

USER appuser

EXPOSE 8110

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8110/healthz', timeout=3); sys.exit(0 if 200 <= r.status < 300 else 1)"

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8110"]
