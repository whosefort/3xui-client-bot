"""WARP-outbound'ы и routing-правила в Marzban Core Settings — тот же
GET-merge-PUT-verify под общим config_lock, что и в reality_admin.py (bulk
API, без ETag/версионирования, лок нужен против lost update).

Важно про архитектуру кластера: core config общий на ВСЕ ноды буквально —
привязать outbound/routing-правило к одной конкретной ноде нельзя, это
потолок самого Marzban. Отдельная WARP-identity на ноду снижает (не
исключает) риск того, что одна и та же identity словит конкурентный
handshake с двух разных физических IP одновременно — обсуждалось и
осознанно принято в чате, не забытый нюанс.
"""
from __future__ import annotations

import re

import aiohttp

from .config import config
from .node_provision import _UA, _marzban_auth, config_lock

_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$")


class WarpAdminError(Exception):
    pass


def valid_domain(domain: str) -> bool:
    return bool(_DOMAIN_RE.match((domain or "").strip()))


async def _get_config(s: aiohttp.ClientSession, headers: dict) -> dict:
    async with s.get(f"{config.marzban_url}/api/core/config", headers=headers) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise WarpAdminError(f"не смог получить core config ({r.status}): {data}")
        return data


async def _put_config(s: aiohttp.ClientSession, headers: dict, cfg: dict) -> None:
    async with s.put(f"{config.marzban_url}/api/core/config", json=cfg, headers=headers) as r:
        res = await r.json(content_type=None)
        if r.status >= 400:
            raise WarpAdminError(f"не смог сохранить core config ({r.status}): {res}")


def _find_outbound(cfg: dict, tag: str) -> dict | None:
    for out in cfg.get("outbounds", []):
        if out.get("tag") == tag:
            return out
    return None


def _find_route_rule(cfg: dict, tag: str) -> dict | None:
    for rule in cfg.get("routing", {}).get("rules", []):
        if rule.get("outboundTag") == tag and rule.get("type") == "field":
            return rule
    return None


async def list_warp_outbounds() -> list[dict]:
    """[{tag, address}, ...] — все wireguard-outbound'ы в конфиге, не только
    WARP теоретически, но у нас в кластере другого источника wireguard нет."""
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
    out = []
    for o in cfg.get("outbounds", []):
        if o.get("protocol") == "wireguard":
            out.append({"tag": o.get("tag"), "address": (o.get("settings") or {}).get("address") or []})
    return out


async def add_warp_outbound(tag: str, data: dict) -> None:
    """data — то, что вернул warp.register(): private_key, address, reserved,
    peer_public_key, peer_endpoint, mtu. Формат outbound'а — 1:1 с тем, что
    строит сам 3x-ui (WarpModal.tsx: collectConfig) — сверено по исходникам,
    не угадано: noKernelTun=true (userspace TUN, kernel требует CAP_NET_ADMIN
    и на многих VPS падает молча) и domainStrategy=ForceIPv4v6 (иначе на
    хосте с половинчатым IPv6 хендшейк молча уходит в чёрную дыру по AAAA)."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)

        if _find_outbound(cfg, tag):
            raise WarpAdminError(f"outbound с тегом {tag!r} уже существует")

        cfg.setdefault("outbounds", []).append({
            "tag": tag,
            "protocol": "wireguard",
            "settings": {
                "mtu": data["mtu"],
                "secretKey": data["private_key"],
                "address": data["address"],
                "reserved": data["reserved"],
                "domainStrategy": "ForceIPv4v6",
                "peers": [{"publicKey": data["peer_public_key"], "endpoint": data["peer_endpoint"]}],
                "noKernelTun": True,
            },
        })
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        if not _find_outbound(verify_cfg, tag):
            raise WarpAdminError("после сохранения outbound не нашёлся — применилось не то, что просили")


async def remove_warp_outbound(tag: str) -> None:
    """Убирает и сам outbound, и его routing-правило (если есть) — иначе
    останется правило, слепо указывающее на несуществующий outbound."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)

        out = _find_outbound(cfg, tag)
        if not out:
            raise WarpAdminError(f"outbound с тегом {tag!r} не найден — список уже изменился")
        cfg["outbounds"].remove(out)

        rule = _find_route_rule(cfg, tag)
        if rule:
            cfg["routing"]["rules"].remove(rule)

        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        if _find_outbound(verify_cfg, tag):
            raise WarpAdminError("после сохранения outbound всё ещё на месте — удаление не применилось")


async def list_warp_routes(tag: str) -> list[str]:
    async with aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)
    rule = _find_route_rule(cfg, tag)
    return list(rule.get("domain", [])) if rule else []


async def add_warp_route(tag: str, domain: str) -> None:
    """Правило добавляется В НАЧАЛО routing.rules (не в конец) — вручную
    выбранный WARP-домен должен побеждать более широкие категорийные
    правила (например rl:rublocktoggle), а не наоборот."""
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)

        if not _find_outbound(cfg, tag):
            raise WarpAdminError(f"outbound {tag!r} не найден — зарегистрируй WARP на ноде сначала")

        rule = _find_route_rule(cfg, tag)
        if rule:
            if domain in rule.get("domain", []):
                return  # уже добавлен, лишний PUT не нужен
            rule.setdefault("domain", []).append(domain)
        else:
            cfg.setdefault("routing", {}).setdefault("rules", []).insert(
                0, {"type": "field", "domain": [domain], "outboundTag": tag}
            )
        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        verify_rule = _find_route_rule(verify_cfg, tag)
        if not verify_rule or domain not in verify_rule.get("domain", []):
            raise WarpAdminError("после сохранения домена нет в правиле — применилось не то, что просили")


async def remove_warp_route(tag: str, domain: str) -> None:
    async with config_lock, aiohttp.ClientSession() as s:
        token = await _marzban_auth(s)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        cfg = await _get_config(s, headers)

        rule = _find_route_rule(cfg, tag)
        if not rule or domain not in rule.get("domain", []):
            return  # уже не там, нечего убирать
        rule["domain"].remove(domain)
        if not rule["domain"]:
            cfg["routing"]["rules"].remove(rule)

        await _put_config(s, headers, cfg)

        verify_cfg = await _get_config(s, headers)
        verify_rule = _find_route_rule(verify_cfg, tag)
        if verify_rule and domain in verify_rule.get("domain", []):
            raise WarpAdminError("после сохранения домен всё ещё в правиле — удаление не применилось")
