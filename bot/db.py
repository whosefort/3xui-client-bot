"""Лёгкий слой БД на стандартном sqlite3.

БД бота — это «правда об идентичности и процессе» (кто привязан, какие заявки,
лог напоминаний, настройки). «Правда о подписке» (срок, трафик) живёт в 3X-UI и
читается из неё на лету. Файл БД содержит sub-ссылки клиентов => относимся к нему
как к секрету (права 600, шифрованные бэкапы).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Optional

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init(db_path: str) -> None:
    global _conn
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    # Файл БД содержит sub-ссылки клиентов (секрет) — жёстко закрываем права
    # на уровне кода, не полагаясь только на дисциплину деплоя (600 = только владелец).
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    _conn.row_factory = sqlite3.Row
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            tg_id        INTEGER PRIMARY KEY,
            tg_username  TEXT,
            client_email TEXT,
            sub_id       TEXT,
            linked_at    INTEGER
        );

        CREATE TABLE IF NOT EXISTS requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id       INTEGER NOT NULL,
            tg_username TEXT,
            type        TEXT NOT NULL,              -- new | renew
            status      TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected
            created_at  INTEGER NOT NULL,
            decided_at  INTEGER,
            admin_id    INTEGER
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Дедупликация напоминаний: одно напоминание на (клиент, дата конца, бакет дней)
        CREATE TABLE IF NOT EXISTS reminders_log (
            tg_id       INTEGER NOT NULL,
            expiry_date TEXT NOT NULL,    -- YYYY-MM-DD конца подписки
            days_before INTEGER NOT NULL,
            sent_at     INTEGER NOT NULL,
            PRIMARY KEY (tg_id, expiry_date, days_before)
        );

        -- Ручная подпись клиента в списках/карточках бота — на случай, когда
        -- клиента выдали вручную и он никогда не писал боту (Marzban/3x-ui не
        -- знают Telegram-юзернеймы вообще, только наш username=u{tg_id}).
        -- Ключ — username в панели (работает и для клиентов без tg_id).
        CREATE TABLE IF NOT EXISTS client_labels (
            client_username TEXT PRIMARY KEY,
            label           TEXT NOT NULL,
            set_by          INTEGER NOT NULL,
            set_at          INTEGER NOT NULL
        );

        -- note: свободное текстовое описание клиента для админа (не подпись,
        -- не показывается клиенту нигде) — добавлено отдельной таблицей, а не
        -- колонкой в client_labels: подпись обязательна там (NOT NULL), а
        -- описание может стоять без подписи и наоборот.
        CREATE TABLE IF NOT EXISTS client_notes (
            client_username TEXT PRIMARY KEY,
            note            TEXT NOT NULL,
            set_by          INTEGER NOT NULL,
            set_at          INTEGER NOT NULL
        );

        -- Одноразовые токены для авторазвёртывания ноды («Добавить сервер»):
        -- бот регистрирует ноду в Marzban и тянет её cert сам, отдаёт токен —
        -- новый VPS забирает cert по токену, пароль от панели никуда не летит.
        CREATE TABLE IF NOT EXISTS node_tokens (
            token       TEXT PRIMARY KEY,
            node_id     INTEGER NOT NULL,
            node_name   TEXT NOT NULL,
            address     TEXT NOT NULL,
            cert_pem    TEXT NOT NULL,
            panel_ip    TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            expires_at  INTEGER NOT NULL,
            used_at     INTEGER,
            created_by  INTEGER NOT NULL
        );
        """
    )
    _conn.commit()


def _c() -> sqlite3.Connection:
    assert _conn is not None, "db.init() не вызван"
    return _conn


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    with _lock:
        row = _c().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        _c().commit()


# ---------- users ----------

def upsert_user(tg_id: int, tg_username: str | None,
                client_email: str | None = None, sub_id: str | None = None) -> None:
    with _lock:
        existing = _c().execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if existing:
            _c().execute(
                "UPDATE users SET tg_username=COALESCE(?,tg_username), "
                "client_email=COALESCE(?,client_email), sub_id=COALESCE(?,sub_id) WHERE tg_id=?",
                (tg_username, client_email, sub_id, tg_id),
            )
        else:
            _c().execute(
                "INSERT INTO users(tg_id,tg_username,client_email,sub_id,linked_at) "
                "VALUES(?,?,?,?,?)",
                (tg_id, tg_username, client_email, sub_id,
                 int(time.time()) if client_email else None),
            )
        _c().commit()


def get_user(tg_id: int) -> Optional[sqlite3.Row]:
    with _lock:
        return _c().execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def all_linked_users() -> list[sqlite3.Row]:
    """Пользователи, у которых есть привязанный клиент (для обхода напоминаний)."""
    with _lock:
        return _c().execute(
            "SELECT * FROM users WHERE client_email IS NOT NULL"
        ).fetchall()


def usernames_map() -> dict[int, str]:
    """tg_id -> @username (или имя), всё что знаем из истории общения с ботом."""
    with _lock:
        rows = _c().execute(
            "SELECT tg_id, tg_username FROM users WHERE tg_username IS NOT NULL"
        ).fetchall()
    return {r["tg_id"]: r["tg_username"] for r in rows}


# ---------- requests ----------

def create_request(tg_id: int, tg_username: str | None, type_: str) -> int:
    with _lock:
        cur = _c().execute(
            "INSERT INTO requests(tg_id,tg_username,type,created_at) VALUES(?,?,?,?)",
            (tg_id, tg_username, type_, int(time.time())),
        )
        _c().commit()
        return cur.lastrowid


def get_request(req_id: int) -> Optional[sqlite3.Row]:
    with _lock:
        return _c().execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()


def pending_requests() -> list[sqlite3.Row]:
    with _lock:
        return _c().execute(
            "SELECT * FROM requests WHERE status='pending' ORDER BY created_at"
        ).fetchall()


def has_pending_request(tg_id: int) -> bool:
    with _lock:
        row = _c().execute(
            "SELECT 1 FROM requests WHERE tg_id=? AND status='pending' LIMIT 1", (tg_id,)
        ).fetchone()
    return row is not None


def decide_request(req_id: int, status: str, admin_id: int) -> None:
    with _lock:
        _c().execute(
            "UPDATE requests SET status=?, decided_at=?, admin_id=? WHERE id=?",
            (status, int(time.time()), admin_id, req_id),
        )
        _c().commit()


def claim_request(req_id: int) -> bool:
    """Атомарно переводит заявку pending -> processing. True, если забрали
    именно мы — иначе кто-то (второй тап, второй админ) уже её обрабатывает.
    Нужно перед любым await к панели: без этого проверка status=='pending' и
    итоговый decide_request() разнесены во времени, и заявку можно одобрить
    дважды (двойная выдача/продление)."""
    with _lock:
        cur = _c().execute(
            "UPDATE requests SET status='processing' WHERE id=? AND status='pending'",
            (req_id,),
        )
        _c().commit()
        return cur.rowcount > 0


def release_request(req_id: int) -> None:
    """Откатывает processing -> pending — вызывать, если после claim_request
    обработка сорвалась (панель недоступна и т.п.), чтобы заявку можно было
    попробовать одобрить повторно."""
    with _lock:
        _c().execute(
            "UPDATE requests SET status='pending' WHERE id=? AND status='processing'",
            (req_id,),
        )
        _c().commit()


# ---------- reminders ----------

def already_reminded(tg_id: int, expiry_date: str, days_before: int) -> bool:
    with _lock:
        row = _c().execute(
            "SELECT 1 FROM reminders_log WHERE tg_id=? AND expiry_date=? AND days_before=?",
            (tg_id, expiry_date, days_before),
        ).fetchone()
    return row is not None


def mark_reminded(tg_id: int, expiry_date: str, days_before: int) -> None:
    with _lock:
        _c().execute(
            "INSERT OR IGNORE INTO reminders_log(tg_id,expiry_date,days_before,sent_at) "
            "VALUES(?,?,?,?)",
            (tg_id, expiry_date, days_before, int(time.time())),
        )
        _c().commit()


# ---------- client_labels (ручная подпись клиента) ----------

def set_client_label(client_username: str, label: str, set_by: int) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO client_labels(client_username,label,set_by,set_at) VALUES(?,?,?,?) "
            "ON CONFLICT(client_username) DO UPDATE SET label=excluded.label, "
            "set_by=excluded.set_by, set_at=excluded.set_at",
            (client_username, label, set_by, int(time.time())),
        )
        _c().commit()


def clear_client_label(client_username: str) -> None:
    with _lock:
        _c().execute("DELETE FROM client_labels WHERE client_username=?", (client_username,))
        _c().commit()


def client_labels_map() -> dict[str, str]:
    """username в панели -> подпись, для всех клиентов у кого она задана."""
    with _lock:
        rows = _c().execute("SELECT client_username, label FROM client_labels").fetchall()
    return {r["client_username"]: r["label"] for r in rows}


# ---------- client_notes (описание клиента для админа) ----------

def set_client_note(client_username: str, note: str, set_by: int) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO client_notes(client_username,note,set_by,set_at) VALUES(?,?,?,?) "
            "ON CONFLICT(client_username) DO UPDATE SET note=excluded.note, "
            "set_by=excluded.set_by, set_at=excluded.set_at",
            (client_username, note, set_by, int(time.time())),
        )
        _c().commit()


def clear_client_note(client_username: str) -> None:
    with _lock:
        _c().execute("DELETE FROM client_notes WHERE client_username=?", (client_username,))
        _c().commit()


def get_client_note(client_username: str) -> Optional[str]:
    with _lock:
        row = _c().execute(
            "SELECT note FROM client_notes WHERE client_username=?", (client_username,)
        ).fetchone()
    return row["note"] if row else None


# ---------- node_tokens (авторазвёртывание нод) ----------

def create_node_token(token: str, node_id: int, node_name: str, address: str,
                      cert_pem: str, panel_ip: str, ttl_seconds: int, created_by: int) -> None:
    now = int(time.time())
    with _lock:
        _c().execute(
            "INSERT INTO node_tokens(token,node_id,node_name,address,cert_pem,panel_ip,"
            "created_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (token, node_id, node_name, address, cert_pem, panel_ip,
             now, now + ttl_seconds, created_by),
        )
        _c().commit()


def claim_node_token(token: str) -> Optional[sqlite3.Row]:
    """Атомарно: если токен валиден и ещё не использован — гасит его и
    возвращает данные. Ни await, ни второй SQL-вызов между проверкой и
    UPDATE не встревает — гонки внутри одного event loop исключены."""
    now = int(time.time())
    with _lock:
        row = _c().execute("SELECT * FROM node_tokens WHERE token=?", (token,)).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] < now:
            return None
        _c().execute("UPDATE node_tokens SET used_at=? WHERE token=?", (now, token))
        _c().commit()
        return row


def purge_old_node_tokens() -> int:
    """Использованные (used_at не пусто) и истёкшие по TTL записи больше не
    нужны — токен одноразовый, второй раз claim_node_token их всё равно не
    отдаст. Просто чистка мусора в БД. Возвращает число удалённых строк."""
    now = int(time.time())
    with _lock:
        cur = _c().execute(
            "DELETE FROM node_tokens WHERE used_at IS NOT NULL OR expires_at < ?", (now,)
        )
        _c().commit()
        return cur.rowcount
