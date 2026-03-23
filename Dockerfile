FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .

RUN mkdir -p /data/fit_files /data/tokens

ENV TRAININGEDGE_DB_PATH=/data/training_edge.db
ENV TRAININGEDGE_FIT_DIR=/data/fit_files
ENV TRAININGEDGE_HOST=0.0.0.0
ENV TRAININGEDGE_PORT=8420
ENV TRAININGEDGE_LOG_FILE=/data/training_edge.log
ENV GARMINTOKENS=/data/tokens

EXPOSE 8420
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8420/api/health')" || exit 1

CMD ["sh", "-c", "python scripts/cli.py init 2>/dev/null; python -m uvicorn api.app:app --host 0.0.0.0 --port 8420"]
