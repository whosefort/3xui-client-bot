FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Каталог под SQLite (монтируется томом из docker-compose)
RUN mkdir -p /app/data

CMD ["python", "-m", "bot.main"]
