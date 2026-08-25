FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data/tmp /app/data/reports /app/data/sessions \
    && chmod 700 /app/data /app/data/tmp /app/data/reports /app/data/sessions

CMD ["python", "-m", "app.main"]
