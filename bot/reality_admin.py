"""Правка REALITY-инбаунда (SNI-камуфляж, ключи, shortId) через Marzban Core
Settings. Конфиг общий на весь кластер — правим один раз, ноды подхватят на
следующем ресинке. Подписка генерируется панелью налету при каждом запросе,
поэтому отдельно рассылать новые ссылки не нужно — клиенты подхватят новые
значения на следующем обновлении подписки в приложении.

Меняем без грейс-периода (осознанный выбор): старые ключи/shortId перестают
работать сразу же, как применили новые — не держим старые вперемешку с
новыми.
"""
from __future__ import annotations

import secrets
import socket
import ssl
from asyncio import gather, to_thread
from base64 import urlsafe_b64encode

import aiohttp
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                            PrivateFormat, PublicFormat)

from .config import config
from .node_provision import _UA, _marzban_auth, config_lock

_DEFAULT_PORT = 443

# Затравка для пикера SNI по конкретной ноде (bot/handlers/admin.py) — не
# гарантия, что подойдёт: check_sni_candidate() всё равно гоняет честный
# TLS1.3-хендшейк при выборе. Просто чтобы не печатать домен руками каждый
# раз — известные, давно живущие сайты с честным TLS1.3 и настоящим CA.
# s0.awsstatic.com (AWS) сюда сознательно не идёт: Amazon — плохой выбор для
# камуфляжа из РФ (см. обсуждение в чате), убран. github.com сюда тоже не
# добавлен: по данным на август 2026 РКН формально не блокирует домен
# целиком, но фиксируется рост аномалий/точечные блокировки IP техдоменов —
# нестабильный кандидат, решать вручную. cisco.com не добавлен: сама Cisco
# геоблокирует часть своих сервисов для трафика из РФ (Cisco Umbrella и
# т.п.) — риск не со стороны РКН, а со стороны самого сайта.
# www.cloudflare.com и www.yahoo.com — не с потолка: нашлись сканом обеих
# нодовых /24-подсетей (RealiTLScanner) и прошли живую проверку
# check_sni_candidate (честный TLS1.3, доверенный CA, без редиректа).
SUGGESTED_DOMAINS = [
    "dl.google.com",
    "www.microsoft.com",
    "addons.mozilla.org",
    "www.swift.org",
    "gitlab.com",
    "www.cloudflare.com",
    "www.yahoo.com",
    "www.speedtest.net",
]

# Готовые порты, которые Cloudflare проксирует без доп. настройки (см.
# node/README.md) — не единственно верные, просто безопасный выбор, чтобы
# не гадать вслепую, какой номер вообще имеет смысл.
SUGGESTED_PORTS = [443, 8443, 2053, 2083, 2087, 2096]

# Затравка для SpiderX — путь в фейковом запросе к сайту-камуфляжу. Формат
# сам по себе не строгий (любая строка с /), но чтобы не мигать одним "/" на
# весь флот, есть из чего выбрать/накидать случайно.
SPX_SUGGESTIONS = ["/", "/url", "/search", "/news", "/images", "/account", "/help", "/about"]


def random_spx() -> str:
    """Не просто рандом из SPX_SUGGESTIONS — иногда собирает похожий на
    реальный путь слаг, чтобы не повторять один и тот же список раз за разом."""
    if secrets.randbelow(2) == 0:
        return secrets.choice(SPX_SUGGESTIONS)
    segments = ["api", "static", "assets", "cdn", "v1", "public", "media", "content"]
    return "/" + secrets.choice(segments) + "/" + secrets.token_hex(3)


class RealityError(Exception):
    pass


# Серверное правило "не пускать .ru/.su через ноду" (доп. страховка поверх
# клиентского Happ-профиля roscomvpn-routing — тот тоже уводит RU-сайты в
# обход VPN, но только если клиент реально применил профиль). Тут — жёсткий
# blackhole на уровне самой ноды, независимо от клиента. Правило узнаём по
# точному списку доменов + outboundTag, чтобы тоггл был идемпотентным.
_RU_BLOCK_DOMAINS = ["geosite:category-ru", "regexp:.*\\.ru$", "regexp:.*\\.su$"]


def _find_ru_block_rule(cfg: dict) -> dict | None:
    for rule in cfg.get("routing", {}).get("rules", []):
        if rule.get("domain") == _RU_BLOCK_DOMAINS and rule.get("outboundTag") == "BLOCK":
            return rule
    return None


async def get_ru_block_enabled() -> bool:
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
    return _find_ru_block_rule(cfg) is not None


async def set_ru_block(enabled: bool) -> None:
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        rules = cfg.setdefault("routing", {}).setdefault("rules", [])
        existing = _find_ru_block_rule(cfg)
        if enabled and not existing:
            rules.append({"type": "field", "domain": list(_RU_BLOCK_DOMAINS), "outboundTag": "BLOCK"})
        elif not enabled and existing:
            rules.remove(existing)
        else:
            return  # уже в нужном состоянии, лишний PUT не нужен
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        now_enabled = _find_ru_block_rule(verify_cfg) is not None
        if now_enabled != enabled:
            raise RealityError("после сохранения состояние правила не совпадает с запрошенным")


def _find_inbound(cfg: dict) -> dict:
    for ib in cfg.get("inbounds", []):
        if ib.get("streamSettings", {}).get("security") == "reality":
            return ib
    raise RealityError("REALITY-инбаунд не найден в конфиге панели")


async def _get_config(s: aiohttp.ClientSession, headers: dict) -> dict:
    async with s.get(f"{config.marzban_url}/api/core/config", headers=headers) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise RealityError(f"не смог получить core config ({r.status}): {data}")
        return data


async def _put_config(s: aiohttp.ClientSession, headers: dict, cfg: dict) -> None:
    async with s.put(f"{config.marzban_url}/api/core/config", json=cfg, headers=headers) as r:
        res = await r.json(content_type=None)
        if r.status >= 400:
            raise RealityError(f"не смог сохранить core config ({r.status}): {res}")


async def get_settings() -> dict:
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
    inbound = _find_inbound(cfg)
    rs = inbound["streamSettings"]["realitySettings"]
    return {
        "dest": rs.get("dest", ""),
        "server_names": rs.get("serverNames", []),
        "public_key": rs.get("publicKey", ""),
        "short_ids": rs.get("shortIds", []),
        "port": inbound.get("port"),
        "spx": rs.get("SpiderX", ""),
    }


async def set_sni(domain: str, port: int = _DEFAULT_PORT) -> None:
    """Меняет камуфляж-домен REALITY (dest + serverNames) и сбрасывает
    per-host override sni в null для всех хостов этого инбаунда — иначе
    хосты с явно выставленным старым sni (см. Hosts API) продолжат отдавать
    клиентам старое значение в обход нового дефолта инбаунда."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}

        cfg = await _get_config(s, headers)
        inbound = _find_inbound(cfg)
        tag = inbound["tag"]
        rs = inbound["streamSettings"]["realitySettings"]
        rs["dest"] = f"{domain}:{port}"
        rs["serverNames"] = [domain]
        await _put_config(s, headers, cfg)

        # Перечитываем и сверяем — PUT мог отдать 200, но не факт, что панель
        # реально сохранила именно то, что просили (гонка с другим
        # админ-действием мимо нашего лока, молчаливое отбрасывание поля и т.п.).
        # Лучше явная ошибка здесь, чем тихий ложный «✅ Применено».
        verify_cfg = await _get_config(s, headers)
        verify_rs = _find_inbound(verify_cfg)["streamSettings"]["realitySettings"]
        if verify_rs.get("dest") != f"{domain}:{port}" or domain not in verify_rs.get("serverNames", []):
            raise RealityError(
                f"после сохранения панель отдаёт другое значение (dest={verify_rs.get('dest')!r}, "
                f"serverNames={verify_rs.get('serverNames')!r}) — применилось не то, что просили"
            )

        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            hosts = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"core config сохранён, но не смог получить hosts ({r.status}): {hosts}")
        changed = False
        for h in hosts.get(tag, []):
            if h.get("sni"):
                h["sni"] = None
                changed = True
        if changed:
            async with s.put(f"{config.marzban_url}/api/hosts", json=hosts, headers=headers) as r:
                res = await r.json(content_type=None)
                if r.status >= 400:
                    raise RealityError(
                        f"core config сохранён, но не смог сбросить sni-override у хостов ({r.status}): {res}"
                    )


# Канонические значения из доки Marzban (валидаторы host-полей). fragment
# режет TLS ClientHello на несколько TCP-сегментов на границе tlshello —
# бьёт сигнатурный DPI, ловящий REALITY одним пакетом. noise хранится на
# хосте на будущее, но реального эффекта СЕЙЧАС не даёт: Marzban кладёт
# noise_setting только в JSON/singbox-конфиг, а не в обычную vless-ссылку,
# которую разбирает Happ — в отличие от fragment (патч в panel/Dockerfile
# чинит ветку reality, у которой апстрим его вообще не проверял).
FRAGMENT_DEFAULT = "10-100,100-200,tlshello"
NOISE_DEFAULT = "rand:10-20,100-200"


async def set_host_fragment(tag: str, index: int, enabled: bool) -> None:
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            hosts = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог получить хосты ({r.status}): {hosts}")
        entries = hosts.get(tag)
        if not entries or index >= len(entries):
            raise RealityError(f"хост {tag}[{index}] не найден — список хостов изменился")
        entries[index]["fragment_setting"] = FRAGMENT_DEFAULT if enabled else None
        entries[index]["noise_setting"] = NOISE_DEFAULT if enabled else None
        async with s.put(f"{config.marzban_url}/api/hosts", json=hosts, headers=headers) as r:
            res = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог сохранить fragment ({r.status}): {res}")


async def set_fragment_all(enabled: bool) -> int:
    """Fragment/noise разом на ВСЕ хосты во всех инбаундах — тот же паттерн,
    что set_fingerprint_all. Возвращает число изменённых записей."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            hosts = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог получить хосты ({r.status}): {hosts}")
        changed = 0
        for entries in hosts.values():
            for h in entries:
                new_frag = FRAGMENT_DEFAULT if enabled else None
                if h.get("fragment_setting") != new_frag:
                    h["fragment_setting"] = new_frag
                    h["noise_setting"] = NOISE_DEFAULT if enabled else None
                    changed += 1
        if changed:
            async with s.put(f"{config.marzban_url}/api/hosts", json=hosts, headers=headers) as r:
                res = await r.json(content_type=None)
                if r.status >= 400:
                    raise RealityError(f"не смог сохранить fragment ({r.status}): {res}")
    return changed


# Marzban ProxyHostFingerprint enum, без "android" — по опыту рунета это
# конкретно тот отпечаток, который чаще палится (андроид-стек TLS менее
# распространён среди обычного веб-трафика, выделяется на общем фоне).
# "randomized" — сам xray на КАЖДОМ соединении берёт случайный отпечаток из
# набора, а не один статичный на всю ноду — сильнее ручной раскидки по
# нодам. Дефолт для всех хостов, если явно не задано другое.
FINGERPRINT_OPTIONS = ["randomized", "chrome", "firefox", "safari", "edge"]


async def set_host_fingerprint(tag: str, index: int, fp: str) -> None:
    if fp not in FINGERPRINT_OPTIONS:
        raise RealityError(f"неизвестный fingerprint: {fp}")
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            hosts = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог получить хосты ({r.status}): {hosts}")
        entries = hosts.get(tag)
        if not entries or index >= len(entries):
            raise RealityError(f"хост {tag}[{index}] не найден — список хостов изменился")
        entries[index]["fingerprint"] = fp
        async with s.put(f"{config.marzban_url}/api/hosts", json=hosts, headers=headers) as r:
            res = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог сохранить fingerprint ({r.status}): {res}")


async def set_fingerprint_all(fp: str) -> int:
    """Фингерпринт разом на ВСЕ хосты во всех инбаундах — не по одному через
    карточку сервера. Возвращает число изменённых записей."""
    if fp not in FINGERPRINT_OPTIONS:
        raise RealityError(f"неизвестный fingerprint: {fp}")
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            hosts = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог получить хосты ({r.status}): {hosts}")
        changed = 0
        for entries in hosts.values():
            for h in entries:
                if h.get("fingerprint") != fp:
                    h["fingerprint"] = fp
                    changed += 1
        if changed:
            async with s.put(f"{config.marzban_url}/api/hosts", json=hosts, headers=headers) as r:
                res = await r.json(content_type=None)
                if r.status >= 400:
                    raise RealityError(f"не смог сохранить fingerprint ({r.status}): {res}")
    return changed


async def set_host_sni(tag: str, index: int, domain: str | None) -> None:
    """SNI для ОДНОГО хоста (нода+инбаунд), не для всего кластера. Это не
    настоящая изоляция — ядро xray у всех нод общее и технически примет
    handshake с ЛЮБЫМ доменом из serverNames, не только со «своим». Но раз
    конкретная нода в ссылках всегда светит только один домен, от пассивного
    SNI-фингерпринтинга (не от целевого пробинга именно этой ноды) помогает.
    domain=None — сброс на общий дефолт инбаунда (serverNames[0]).
    Если domain не входит в текущий serverNames — молча добавляет его туда
    (не заменяет остальные, только дописывает)."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}

        if domain:
            cfg = await _get_config(s, headers)
            rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
            if domain not in rs.get("serverNames", []):
                rs.setdefault("serverNames", []).append(domain)
                await _put_config(s, headers, cfg)

        async with s.get(f"{config.marzban_url}/api/hosts", headers=headers) as r:
            hosts = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог получить хосты ({r.status}): {hosts}")
        entries = hosts.get(tag)
        if not entries or index >= len(entries):
            raise RealityError(f"хост {tag}[{index}] не найден — список хостов изменился")
        entries[index]["sni"] = domain
        async with s.put(f"{config.marzban_url}/api/hosts", json=hosts, headers=headers) as r:
            res = await r.json(content_type=None)
            if r.status >= 400:
                raise RealityError(f"не смог сохранить sni хоста ({r.status}): {res}")


async def regenerate_keys() -> str:
    """Новая X25519-пара в формате xray REALITY (raw 32 байта, base64
    urlsafe без паддинга). Возвращает новый publicKey (безопасно показать)."""
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    priv_b64 = urlsafe_b64encode(priv_raw).rstrip(b"=").decode()
    pub_b64 = urlsafe_b64encode(pub_raw).rstrip(b"=").decode()

    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
        rs["privateKey"] = priv_b64
        rs["publicKey"] = pub_b64
        await _put_config(s, headers, cfg)
    return pub_b64


async def regenerate_short_ids(count: int = 8) -> list[str]:
    ids = [secrets.token_hex(4) for _ in range(count)]  # 8 hex-символов каждый
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
        rs["shortIds"] = ids
        await _put_config(s, headers, cfg)
    return ids


async def add_short_id() -> str:
    """Добавляет ОДИН shortId, не трогая остальные — мягкая ротация: новый
    работает сразу, старые продолжают работать, пока их явно не убрали."""
    new_id = secrets.token_hex(4)
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
        rs.setdefault("shortIds", []).append(new_id)
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        verify_rs = _find_inbound(verify_cfg)["streamSettings"]["realitySettings"]
        if new_id not in verify_rs.get("shortIds", []):
            raise RealityError("после сохранения новый shortId не найден в конфиге")
    return new_id


async def remove_short_id(short_id: str) -> None:
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
        ids = rs.get("shortIds", [])
        if short_id not in ids:
            raise RealityError(f"shortId {short_id} не найден — список уже изменился")
        if len(ids) <= 1:
            raise RealityError("нельзя убрать последний shortId — REALITY требует хотя бы один")
        ids.remove(short_id)
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        verify_rs = _find_inbound(verify_cfg)["streamSettings"]["realitySettings"]
        if short_id in verify_rs.get("shortIds", []):
            raise RealityError("после сохранения shortId всё ещё в конфиге — не удалился")


async def set_port(port: int) -> None:
    """Порт REALITY-инбаунда на весь кластер. Hosts у нас port=null (см.
    /api/hosts) — это значит «наследовать порт инбаунда», так что для
    подписки ничего больше трогать не нужно. НО: порт на нодах открывает UFW
    (см. node/bootstrap.sh, INBOUND_PORTS) — эта функция его не трогает,
    новый порт на уже развёрнутых нодах нужно открыть в файрволе руками."""
    if not (1 <= port <= 65535):
        raise RealityError(f"порт {port} вне диапазона 1-65535")
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        inbound = _find_inbound(cfg)
        inbound["port"] = port
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        if _find_inbound(verify_cfg).get("port") != port:
            raise RealityError("после сохранения порт в конфиге не совпадает с запрошенным")


async def set_spx(value: str) -> None:
    """SpiderX — путь в фейковом запросе к сайту-камуфляжу (часть
    правдоподобия REALITY, см. app/xray/config.py в Marzban). Пустая строка
    сбрасывает на дефолт (панель сама подставит '')."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
        rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
        if value:
            rs["SpiderX"] = value
        else:
            rs.pop("SpiderX", None)
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        verify_rs = _find_inbound(verify_cfg)["streamSettings"]["realitySettings"]
        if verify_rs.get("SpiderX", "") != value:
            raise RealityError("после сохранения SpiderX в конфиге не совпадает с запрошенным")


def _check_redirect(tls: ssl.SSLSocket, domain: str, timeout: float) -> str | None:
    """Тянет '/' по уже открытому TLS-сокету и смотрит статус-код. Редирект
    с главной — минус к правдоподобию камуфляжа (реальные сайты обычно
    отвечают 200 на свою же главную). Только для http/1.1 — h2 бинарный,
    руками не распарсить без отдельной библиотеки, тогда просто не проверяем."""
    try:
        tls.settimeout(timeout)
        tls.sendall(
            f"GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\nUser-Agent: Mozilla/5.0\r\n\r\n".encode()
        )
        status_line = b""
        while b"\r\n" not in status_line and len(status_line) < 256:
            chunk = tls.recv(256)
            if not chunk:
                break
            status_line += chunk
        first_line = status_line.split(b"\r\n", 1)[0].decode(errors="replace")
        parts = first_line.split(maxsplit=2)
        code = parts[1] if len(parts) > 1 else ""
        return code if code.startswith("3") else None
    except (OSError, ssl.SSLError, UnicodeDecodeError):
        return None  # не смогли проверить — не блокируем кандидата из-за этого


def _probe_tls13(domain: str, port: int, timeout: float) -> dict:
    """Блокирующая часть — гоняется через to_thread. Настоящий TLS1.3-хендшейк
    с проверкой сертификата (не self-signed, реальный CA) — REALITY-камуфляж
    работает только на сайтах, которые честно говорят TLS1.3 и имеют
    доверенный серт (иначе хендшейк-имитация не пройдёт проверку клиента)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    redirect_code = None
    try:
        with socket.create_connection((domain, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                version = tls.version()
                alpn = tls.selected_alpn_protocol()
                cert = tls.getpeercert()
                if alpn != "h2":
                    redirect_code = _check_redirect(tls, domain, timeout)
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "reason": f"серт не прошёл проверку: {e.verify_message}"}
    except (ssl.SSLError, socket.error, OSError) as e:
        return {"ok": False, "reason": f"не удалось подключиться: {e}"}

    if version != "TLSv1.3":
        return {"ok": False, "reason": f"домен ответил {version}, а не TLS 1.3 — REALITY не пройдёт"}
    if redirect_code:
        return {"ok": False, "reason": f"главная страница редиректит (HTTP {redirect_code}) — не похоже на честный сайт"}

    issuer = dict(x[0] for x in cert.get("issuer", [])) if cert else {}
    return {
        "ok": True,
        "tls_version": version,
        "alpn": alpn or "—",
        "issuer": issuer.get("organizationName", issuer.get("commonName", "—")),
    }


async def check_sni_candidate(domain: str, port: int = _DEFAULT_PORT, timeout: float = 6.0) -> dict:
    """Проверка домена-кандидата на пригодность для REALITY-камуфляжа ДО
    применения — плохой выбор (не TLS1.3, самоподписанный серт) ломает
    хендшейк для всех клиентов сразу, лучше поймать здесь."""
    return await to_thread(_probe_tls13, domain, port, timeout)


async def validate_candidates(domains: list[str]) -> list[str]:
    """Прогоняет check_sni_candidate по всем сразу (параллельно) и возвращает
    только те, что реально прошли — не предлагаем в пикере то, что уже не
    годится. Список короткий (обычно SUGGESTED_DOMAINS, ~6 штук) — намеренно
    не масштабируем на произвольные объёмы: пара TLS-хендшейков с мастера на
    известные публичные сайты не выглядит сканом, десятки/сотни — уже похоже."""
    results = await gather(*(check_sni_candidate(d) for d in domains), return_exceptions=True)
    return [d for d, r in zip(domains, results) if isinstance(r, dict) and r.get("ok")]
