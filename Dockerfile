FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/subsidy.db \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && useradd --create-home --uid 10001 app \
    && chown -R app:app /app /data

USER app
EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "60", "wsgi:app"]
