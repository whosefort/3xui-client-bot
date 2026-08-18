FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# pyrage (age-шифрование бэкапа) — best-effort: если под арх нет готового wheel,
# НЕ валим сборку. Без него бэкап работает, но без шифрования (статус это покажет).
RUN pip install --no-cache-dir "pyrage>=1.1,<2" \
    || echo "ВНИМАНИЕ: pyrage не установлен — бэкап будет без age-шифрования"

COPY bot/ ./bot/
# Только пин-файлы версий (не весь node/) — «Добавить сервер» отдаёт новым
# нодам те же проверенные версии, что и node/bootstrap.sh на месте.
COPY node/XRAY_VERSION ./node/XRAY_VERSION
COPY node/MARZBAN_NODE_IMAGE ./node/MARZBAN_NODE_IMAGE

# Непривилегированный пользователь (uid 10001). Контейнер не работает от root,
# чтобы при компрометации зависимости минимизировать радиус поражения на host
# (особенно с network_mode: host). Каталог data монтируется томом — права на
# него синхронизирует deploy.sh (chown 10001 на host-стороне).
RUN useradd -u 10001 -r -s /usr/sbin/nologin -d /app appuser \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app

USER 10001

CMD ["python", "-m", "bot.main"]
