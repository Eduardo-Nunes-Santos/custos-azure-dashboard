FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo \
    OUTPUT_DIR=/data \
    REFRESH_MINUTES=60 \
    MESES_HISTORICO=3 \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY azure_dashboard.py template.html entrypoint.sh ./
COPY vendor ./vendor

RUN chmod +x entrypoint.sh \
    && useradd -r -u 1000 app \
    && mkdir -p /data/www && chown -R app /data

USER app
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
