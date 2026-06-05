FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Непривилегированный пользователь (uid 10001). Контейнер не работает от root,
# чтобы при компрометации зависимости минимизировать радиус поражения на host
# (особенно с network_mode: host). Каталог data монтируется томом — права на
# него синхронизирует deploy.sh (chown 10001 на host-стороне).
RUN useradd -u 10001 -r -s /usr/sbin/nologin -d /app appuser \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app

USER 10001

CMD ["python", "-m", "bot.main"]
