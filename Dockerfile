FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN addgroup --system arda && adduser --system --ingroup arda arda

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY deploy ./deploy

RUN mkdir -p output/state output/logs && chown -R arda:arda /app

USER arda
EXPOSE 8888

CMD ["python", "app/main.py", "serve", "--host", "0.0.0.0", "--port", "8888"]
