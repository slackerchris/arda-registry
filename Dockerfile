FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN addgroup --system arda \
    && adduser --system --ingroup arda arda \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY data ./default-data
COPY deploy ./deploy
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p output/state output/logs \
    && chown -R arda:arda /app

EXPOSE 8888

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "app/main.py", "serve", "--host", "0.0.0.0", "--port", "8888"]
