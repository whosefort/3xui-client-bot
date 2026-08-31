"""Единственный входящий HTTP-эндпоинт бота: выдача cert новой ноды по
одноразовому токену («Добавить сервер» → node/bootstrap_token.sh).

Бот всегда работал через long-polling именно чтобы не иметь входящих
портов вообще (см. bot/main.py) — это осознанное исключение из этого
правила, поэтому: включается только явным NODE_PROVISION_ENABLED=true,
единственный маршрут, токен одноразовый и короткоживущий, тело ответа не
содержит ничего кроме того, что и так утекло бы через сам bootstrap.sh при
ручном разворачивании (client-cert ноды, не пароль от панели).
"""
from __future__ import annotations

import logging
import os
import ssl

import aiohttp
from aiohttp import web

from . import db, ssh_ops
from .config import config

log = logging.getLogger("http_api")

_XRAY_VERSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node", "XRAY_VERSION"
)
_NODE_IMAGE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node", "MARZBAN_NODE_IMAGE"
)
# см. docker-compose.yml — тот же серт, что панель уже использует на 443.
_CERT_DIR = "/app/marzban-certs"
_CERT_FILE = os.path.join(_CERT_DIR, "fullchain.pem")
_KEY_FILE = os.path.join(_CERT_DIR, "key.pem")


def _read_xray_version() -> str:
    try:
        with open(_XRAY_VERSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_node_image() -> str:
    try:
        with open(_NODE_IMAGE_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


async def _fetch_latest_stable_xray_version() -> str:
    """GitHub /releases/latest сам отфильтровывает pre-release — в отличие
    от node/XRAY_VERSION, который может быть pin'ут на pre-release тег (см.
    bot/handlers/admin.py — та же логика для флот-апгрейда). Пусто при любой
    ошибке: bootstrap_token.sh тогда падает на свой штатный GitHub-latest
    фолбэк (уже включая pre-release) — не идеально, но не блокирует бутстрап."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.github.com/repos/XTLS/Xray-core/releases/latest",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json(content_type=None)
                if r.status >= 400 or not data:
                    return ""
                return data.get("tag_name") or ""
    except Exception:  # noqa: BLE001
        log.exception("не смог получить latest stable xray-core с GitHub при claim")
        return ""


async def _claim(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)

    token = (body or {}).get("token", "")
    if not token:
        return web.json_response({"error": "no token"}, status=400)

    row = db.claim_node_token(token)
    if not row:
        return web.json_response({"error": "invalid, expired or already used token"}, status=403)

    log.info("нода id=%s (%s) забрала cert по токену", row["node_id"], row["address"])
    xray_channel = row["xray_channel"] if "xray_channel" in row.keys() else "stable"
    if xray_channel == "latest":
        # Намеренно отдаём пустой xray_version: у bootstrap_token.sh уже есть
        # штатный фолбэк "пусто -> спросить GitHub latest сам", не дублируем
        # этот запрос здесь.
        xray_version = ""
    elif xray_channel == "laststable":
        xray_version = await _fetch_latest_stable_xray_version()
    else:
        xray_version = _read_xray_version()
    return web.json_response({
        "node_id": row["node_id"],
        "node_name": row["node_name"],
        "address": row["address"],
        "cert_pem": row["cert_pem"],
        "panel_ip": row["panel_ip"],
        "xray_version": xray_version,
        "node_image": _read_node_image(),
        "bot_ssh_pubkey": ssh_ops.ensure_keypair(),
    })


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/nodeprovision/claim", _claim)
    app.router.add_get("/nodeprovision/health", _health)
    return app


def _build_ssl_context() -> ssl.SSLContext:
    """Fail closed: этот эндпоинт отдаёт cert_pem новой ноды — долгоживущий
    mTLS-кред, не токен с TTL. Без TLS сюда никак нельзя пускать трафик,
    поэтому при отсутствии сертификата падаем со стартом бота целиком, а не
    тихо поднимаем порт голым HTTP (раньше было именно так — легко было не
    заметить log.error и словить реальную утечку через CF Flexible SSL)."""
    if not (os.path.isfile(_CERT_FILE) and os.path.isfile(_KEY_FILE)):
        raise RuntimeError(
            f"NODE_PROVISION_ENABLED=true, но нет {_CERT_FILE}/{_KEY_FILE} "
            f"(см. MARZBAN_CERT_DIR в docker-compose.yml) — отдавать cert_pem ноды "
            f"голым HTTP нельзя, отказываюсь стартовать. Либо почини монтирование "
            f"сертификата, либо выключи NODE_PROVISION_ENABLED."
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(_CERT_FILE, _KEY_FILE)
    return ctx


async def start(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx = _build_ssl_context()
    site = web.TCPSite(runner, "0.0.0.0", config.node_provision_port, ssl_context=ssl_ctx)
    await site.start()
    log.info("HTTP-эндпоинт провижининга нод слушает :%d (tls=on)",
             config.node_provision_port)
    return runner
