"""Суточный бэкап БД в Cloudflare R2 (S3-совместимое объектное хранилище).

Опционально (BACKUP_ENABLED). Что делает:
1. Снимает консистентный онлайн-снимок sqlite (bot.db + x-ui.db, оба читаются RO).
2. Опционально шифрует age-публичным ключом (приватный — только у владельца).
3. Заливает в R2 отдельным объектом с датой в имени.

Ротацию НЕ делаем из кода — у R2-токена должны быть права только на запись,
а старое подчищает lifecycle-правило бакета. Так компрометация сервера не даёт
стереть историю бэкапов.

boto3 и pyrage импортируются лениво: их отсутствие/проблема сборки не валит бота,
а лишь отключает фичу с понятным сообщением.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import time

from . import db
from .config import config

log = logging.getLogger("backup")

# Путь к x-ui.db ВНУТРИ контейнера (монтируется RO из docker-compose).
XUI_DB_PATH = os.getenv("XUI_DB_PATH", "/backup-src/x-ui.db")


def effective_enabled() -> bool:
    """Мастер-флаг (.env) И отсутствие рантайм-паузы (one-click из админки)."""
    return config.backup_enabled and db.get_setting("backup_paused", "0") != "1"


def _human(n: int) -> str:
    f = float(n)
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if f < 1024:
            return f"{f:.0f} {u}" if u == "Б" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} ТБ"


def _snapshot(src_path: str, dst_path: str) -> None:
    """Консистентный онлайн-снимок sqlite. Источник открываем read-only —
    безопасно даже пока 3X-UI/бот пишут в свою БД из другого процесса."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _maybe_encrypt(path: str) -> tuple[str, bool]:
    """Зашифровать age-публичным ключом, если он задан. (путь, зашифровано?)."""
    pubkey = (config.backup_age_pubkey or "").strip()
    if not pubkey:
        return path, False
    try:
        import pyrage  # ленивый импорт
        recipient = pyrage.x25519.Recipient.from_str(pubkey)
        with open(path, "rb") as f:
            data = f.read()
        enc = pyrage.encrypt(data, [recipient])
        out = path + ".age"
        with open(out, "wb") as f:
            f.write(enc)
        os.remove(path)
        return out, True
    except Exception as e:  # noqa: BLE001
        log.error("age-шифрование не удалось (%s) — заливаю без шифрования", e)
        return path, False


def _upload(local_path: str, key: str) -> None:
    import boto3  # ленивый импорт
    from botocore.config import Config as BotoConfig
    s3 = boto3.client(
        "s3",
        endpoint_url=config.r2_endpoint,
        aws_access_key_id=config.r2_access_key_id,
        aws_secret_access_key=config.r2_secret_access_key,
        region_name="auto",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )
    s3.upload_file(local_path, config.r2_bucket, key)


def _r2_configured() -> bool:
    return bool(config.r2_endpoint and config.r2_bucket
               and config.r2_access_key_id and config.r2_secret_access_key)


# Защита от наложения: ручной /backup (или кнопка) и суточный запуск не должны
# идти одновременно.
_lock = asyncio.Lock()


async def run_backup() -> str:
    """Сделать бэкап и залить в R2. Возвращает короткий статус для отбивки/админки.
    Никогда не бросает — всё заворачиваем, чтобы вызыватель (heartbeat) не падал."""
    if not config.backup_enabled:
        return "выключен (.env)"
    if db.get_setting("backup_paused", "0") == "1":
        return "⏸ на паузе"
    if not _r2_configured():
        return "⚠️ R2 не настроен (.env)"
    if _lock.locked():
        return "уже выполняется"

    async with _lock:
        expect_enc = bool((config.backup_age_pubkey or "").strip())
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        ts = int(time.time())
        sources = [("bot", config.db_path)]
        if os.path.exists(XUI_DB_PATH):
            sources.append(("x-ui", XUI_DB_PATH))
        else:
            log.warning("x-ui.db не найден по %s — бэкаплю только bot.db", XUI_DB_PATH)

        tmpdir = None
        parts, enc_count = [], 0
        try:
            tmpdir = tempfile.mkdtemp(prefix="bk_")
            for name, src in sources:
                snap = os.path.join(tmpdir, f"{name}.db")
                await asyncio.to_thread(_snapshot, src, snap)
                final, enc = await asyncio.to_thread(_maybe_encrypt, snap)
                if enc:
                    enc_count += 1
                size = os.path.getsize(final)
                key = f"backups/{stamp}/{name}-{ts}.db" + (".age" if enc else "")
                await asyncio.to_thread(_upload, final, key)
                parts.append(f"{name} {_human(size)}")
            # Громко сигналим, если ждали шифрование, но что-то ушло открытым текстом.
            if expect_enc and enc_count < len(sources):
                return ("⚠️ ЗАЛИТО БЕЗ ШИФРОВАНИЯ — проверь age-ключ/pyrage! "
                        + ", ".join(parts))
            return f"✅ R2 {'🔒' if enc_count else '🔓'} " + ", ".join(parts)
        except Exception as e:  # noqa: BLE001
            log.exception("backup failed")
            return f"⚠️ ошибка: {type(e).__name__}"
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
