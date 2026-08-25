"""Регистрация бесплатной Cloudflare WARP identity — без аккаунта, без
клиента Cloudflare, без внешних бинарей (wgcf и т.п.). Ровно то, что делает
3x-ui у себя (internal/web/service/integration/warp.go): один POST на
официальный (но нигде не документированный публично) эндпоинт регистрации
WARP-мобильных клиентов, с самостоятельно сгенерированной WireGuard-парой
ключей. Ключи WireGuard — тот же Curve25519, что и REALITY, но кодируются
СТАНДАРТНЫМ base64 с паддингом (не urlsafe-без-паддинга, как в
reality_admin.py) — это формат, которого ждёт сам xray-core в wireguard-outbound.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from datetime import datetime, timezone

import aiohttp
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption,
                                                            PrivateFormat, PublicFormat)

_API_BASE = "https://api.cloudflareclient.com/v0a4005"
_CLIENT_VER = "a-6.30-3596"
_MTU = 1420


class WarpError(Exception):
    pass


def _keypair() -> tuple[str, str]:
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return b64encode(priv_raw).decode(), b64encode(pub_raw).decode()


async def register(label: str) -> dict:
    """Регистрирует новую WARP-identity. Возвращает всё нужное для
    wireguard-outbound в core config: tag-friendly label, private_key,
    address (список CIDR), reserved (байты client_id), peer_public_key,
    peer_endpoint, mtu. Кидает WarpError с текстом причины при любом сбое —
    Cloudflare иногда отдаёт понятный message в errors[]."""
    priv_b64, pub_b64 = _keypair()
    body = {
        "key": pub_b64,
        "tos": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "type": "PC",
        "model": "x-ui",
        "name": label,
    }
    headers = {"CF-Client-Version": _CLIENT_VER, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{_API_BASE}/reg", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                msg = ((data or {}).get("errors") or [{}])[0].get("message")
                raise WarpError(f"регистрация WARP не удалась ({r.status}): {msg or data}")

    conf = data.get("config") or {}
    peers = conf.get("peers") or []
    if not peers:
        raise WarpError(f"Cloudflare не вернул peer в ответе: {data}")
    peer = peers[0]
    endpoint = (peer.get("endpoint") or {}).get("host")
    peer_pub = peer.get("public_key")
    if not endpoint or not peer_pub:
        raise WarpError(f"в ответе нет endpoint/public_key пира: {data}")

    addrs = (conf.get("interface") or {}).get("addresses") or {}
    address = []
    if addrs.get("v4"):
        address.append(f"{addrs['v4']}/32")
    if addrs.get("v6"):
        address.append(f"{addrs['v6']}/128")
    if not address:
        raise WarpError(f"в ответе нет ни одного адреса интерфейса: {data}")

    client_id = conf.get("client_id") or ""
    reserved = list(b64decode(client_id)) if client_id else []

    return {
        "private_key": priv_b64,
        "address": address,
        "reserved": reserved,
        "peer_public_key": peer_pub,
        "peer_endpoint": endpoint,
        "mtu": _MTU,
        "device_id": data.get("id"),
    }
