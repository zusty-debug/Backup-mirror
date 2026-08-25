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

# Persist boot/runtime output so it remains inspectable from the host even if the process exits.
CMD ["sh", "-c", "echo \"$(date -Iseconds) launching telegram-media-mirror\" >> /app/data/bot.log; exec python -m app.main >> /app/data/bot.log 2>&1"]
