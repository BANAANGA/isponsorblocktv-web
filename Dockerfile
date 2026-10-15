FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ISPBTV_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY webui ./webui
RUN pip install --no-cache-dir . && \
    useradd --system --uid 10001 --create-home webapp && \
    mkdir -p /data && chown webapp:webapp /data

USER webapp
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/auth/status', timeout=2)"
CMD ["python", "-m", "uvicorn", "webui.app:app", "--host", "0.0.0.0", "--port", "8000"]
