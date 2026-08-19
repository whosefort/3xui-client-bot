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
from asyncio import to_thread
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
SUGGESTED_DOMAINS = [
    "dl.google.com",
    "www.microsoft.com",
    "addons.mozilla.org",
    "www.swift.org",
    "s0.awsstatic.com",
    "www.speedtest.net",
]


class RealityError(Exception):
    pass


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
    rs = _find_inbound(cfg)["streamSettings"]["realitySettings"]
    return {
        "dest": rs.get("dest", ""),
        "server_names": rs.get("serverNames", []),
        "public_key": rs.get("publicKey", ""),
        "short_ids": rs.get("shortIds", []),
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


# Marzban ProxyHostFingerprint enum. "randomized" — сам xray на КАЖДОМ
# соединении берёт случайный отпечаток из набора, а не один статичный на
# всю ноду — сильнее ручной раскидки по нодам (та фиксирована и тоже в
# итоге фингерпринтится, просто на уровне "одна нода = один fp"). Дефолт
# для всех хостов, если явно не задано другое.
FINGERPRINT_OPTIONS = ["randomized", "chrome", "firefox", "safari", "ios", "android", "edge"]


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


def _probe_tls13(domain: str, port: int, timeout: float) -> dict:
    """Блокирующая часть — гоняется через to_thread. Настоящий TLS1.3-хендшейк
    с проверкой сертификата (не self-signed, реальный CA) — REALITY-камуфляж
    работает только на сайтах, которые честно говорят TLS1.3 и имеют
    доверенный серт (иначе хендшейк-имитация не пройдёт проверку клиента)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    try:
        with socket.create_connection((domain, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                version = tls.version()
                alpn = tls.selected_alpn_protocol()
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "reason": f"серт не прошёл проверку: {e.verify_message}"}
    except (ssl.SSLError, socket.error, OSError) as e:
        return {"ok": False, "reason": f"не удалось подключиться: {e}"}

    if version != "TLSv1.3":
        return {"ok": False, "reason": f"домен ответил {version}, а не TLS 1.3 — REALITY не пройдёт"}

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
