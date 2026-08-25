"""Обёртка над XTLS/RealiTLScanner — сканирует IP/подсеть/домен и находит
сайты, пригодные для REALITY-камуфляжа (честный TLS1.3, доверенный серт).
Результат — сырые кандидаты; финальная проверка перед применением всё равно
идёт через reality_admin.check_sni_candidate (тот же хендшейк-тест, что и
для домена, введённого руками).

Бинарь скачивается один раз (пин версии — see _SCANNER_VERSION) в scratch-
каталог и переиспользуется. Раз мы уже внутри контейнера бота на мастере —
никакого SSH, просто локальный subprocess.
"""
from __future__ import annotations

import asyncio
import csv
import ipaddress
import logging
import os
import re
import stat
import uuid

import aiohttp

log = logging.getLogger("reality_scan")

_SCANNER_VERSION = "v0.2.3"
_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_BIN_DIR = "/tmp/realitlscanner"
_BIN_PATH = os.path.join(_BIN_DIR, f"scanner-{_SCANNER_VERSION}")
_MAX_SCAN_SECONDS = 180
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$")


class ScanError(Exception):
    pass


def _validate_target(target: str) -> None:
    """Только IP, CIDR (не крупнее /24) или домен — то, что реально уйдёт в
    -addr сканера как есть. Отдельно защищаемся от «-что-то», что могло бы
    подсунуться сканеру как ещё один флаг (аргументы идут списком, не через
    shell, но форма всё равно должна быть похожа на реальную цель)."""
    target = target.strip()
    if not target or target.startswith("-"):
        raise ScanError("Пустая или подозрительная цель.")
    if "/" in target:
        try:
            net = ipaddress.ip_network(target, strict=False)
        except ValueError:
            raise ScanError(f"«{target}» не похоже на CIDR.")
        max_prefix = 24 if net.version == 4 else 120
        if net.prefixlen < max_prefix:
            raise ScanError(f"Диапазон слишком большой — не крупнее /{max_prefix}.")
        return
    try:
        ipaddress.ip_address(target)
        return
    except ValueError:
        pass
    if _DOMAIN_RE.match(target):
        return
    raise ScanError(f"«{target}» не похоже на IP, CIDR или домен.")


def _arch() -> str:
    machine = os.uname().machine
    arch = _ARCH_MAP.get(machine)
    if not arch:
        raise ScanError(f"Неизвестная архитектура {machine} — нет готового бинаря RealiTLScanner.")
    return arch


async def _ensure_binary() -> str:
    if os.path.exists(_BIN_PATH):
        return _BIN_PATH
    os.makedirs(_BIN_DIR, exist_ok=True)
    url = (
        f"https://github.com/XTLS/RealiTLScanner/releases/download/"
        f"{_SCANNER_VERSION}/RealiTLScanner-linux-{_arch()}"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 400:
                raise ScanError(f"не смог скачать сканер ({r.status}): {url}")
            data = await r.read()
    tmp = _BIN_PATH + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, _BIN_PATH)
    return _BIN_PATH


async def scan(target: str, *, thread: int = 50, timeout_s: int = 8, max_results: int = 10) -> list[dict]:
    """Возвращает [{domain, issuer, geo}, ...] — уникальные CERT_DOMAIN,
    отсортированные как отдал сканер. Пустой список — ничего не нашёл."""
    _validate_target(target)
    binary = await _ensure_binary()

    # uuid4, не hash(target)+pid — оба детерминированы в рамках процесса,
    # два параллельных скана ОДНОЙ цели получили бы один и тот же путь и
    # гонялись бы за один файл (запись/чтение/удаление вперемешку).
    out_csv = f"{_BIN_DIR}/out-{uuid.uuid4().hex}.csv"
    args = [
        binary, "-addr", target, "-port", "443",
        "-thread", str(thread), "-timeout", str(timeout_s), "-out", out_csv,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=_MAX_SCAN_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ScanError(f"Скан не уложился в {_MAX_SCAN_SECONDS} сек — цель слишком большая, сузь диапазон.")

    if not os.path.exists(out_csv):
        return []
    try:
        with open(out_csv, newline="") as f:
            rows = list(csv.reader(f))
    finally:
        os.remove(out_csv)

    seen: set[str] = set()
    results = []
    for row in rows:
        # Реальный формат RealiTLScanner v0.2.3 (проверено вживую):
        # IP,ORIGIN,TLS,ALPN,CURVE,CERT_LENGTH,CERT_SIGNATURE,CERT_PUBLICKEY,
        # CERT_DOMAIN,CERT_ISSUER,GEO_CODE — 11 колонок, домен/issuer/geo на
        # позициях 8/9/10, не 2/3/4. Плюс файл начинается с заголовка (IP,...) —
        # без обеих поправок сюда попадали "TLS"/"ALPN"/"CURVE" как fake-домены.
        if row and row[0] == "IP":
            continue
        if len(row) < 11:
            continue
        cert_domain, cert_issuer, geo = row[8], row[9], row[10]
        cert_domain = cert_domain.strip().lstrip("*.")
        if not cert_domain or cert_domain in seen:
            continue
        seen.add(cert_domain)
        results.append({"domain": cert_domain, "issuer": cert_issuer.strip(), "geo": geo.strip()})
        if len(results) >= max_results:
            break
    return results
