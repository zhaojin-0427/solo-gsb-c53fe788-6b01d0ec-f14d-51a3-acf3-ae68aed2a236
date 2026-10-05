FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ADJ_DB_PATH=/data/adjudication.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# 使用 lifespan 之外的显式初始化（见下方命令）
CMD ["sh", "-c", "python -c \"from app import database; database.init_db()\" && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
