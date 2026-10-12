"""Happ routing-профиль (заголовок `routing` на sub.-домене) с доменами,
которые админ добавил из бота поверх базового panel/happ_routing.deeplink.

Раньше deeplink был статически впаян в /etc/caddy/Caddyfile — любая правка
требовала sed по конфигу + reload Caddy руками по SSH. Теперь Caddy читает
значение из файла плейсхолдером {file.…} на каждый запрос, а бот этот файл
пересобирает: база из репо + свои direct-домены из БД. Ни reload, ни доступа
бота к Caddy не нужно — только общий каталог на хосте (см. docker-compose.yml,
panel/caddy/setup.sh).

Базовый профиль НЕ редактируется из бота: свои домены лежат отдельно в
settings, поэтому обновление базы из репо их не затирает, а удалить из бота
можно только то, что из бота же и добавили.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from . import db

log = logging.getLogger("happ_routing")

_PREFIX = "happ://routing/onadd/"
_BASE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "panel", "happ_routing.deeplink"
)
_OUT_PATH = os.getenv("HAPP_ROUTING_OUT", "/app/routing/happ_routing.deeplink")
_SETTING_KEY = "direct_domains"
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# Заголовок целиком должен пролезать в типовые лимиты (8 КБ на строку
# заголовка у большинства клиентов/прокси) — база ~1.7 КБ, остальное под свои.
_MAX_DEEPLINK_LEN = 6000


class RoutingError(Exception):
    pass


def _load_base() -> dict:
    try:
        with open(_BASE_PATH) as f:
            content = f.read().strip()
    except OSError as e:
        raise RoutingError(f"не найден базовый профиль {_BASE_PATH}: {e}") from e
    if not content.startswith(_PREFIX):
        raise RoutingError("базовый профиль не похож на happ://routing/onadd/…")
    b64 = content[len(_PREFIX):]
    try:
        return json.loads(urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    except ValueError as e:
        raise RoutingError(f"базовый профиль не декодируется: {e}") from e


def base_direct() -> list[str]:
    """DirectSites базового профиля — только показать админу, не для правки."""
    return list(_load_base().get("DirectSites", []))


def custom_domains() -> list[str]:
    raw = db.get_setting(_SETTING_KEY, "[]")
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [d for d in data if isinstance(d, str)]


def normalize(text: str) -> tuple[list[str], list[str]]:
    """Разбирает ввод админа: домены через пробел/запятую/перенос, можно со
    схемой и путём (вставил ссылку целиком — ок). Возвращает (годные, мусор)."""
    good: list[str] = []
    bad: list[str] = []
    for tok in re.split(r"[\s,;]+", text or ""):
        if not tok:
            continue
        d = tok.strip().lower()
        d = re.sub(r"^[a-z]+://", "", d)
        d = d.split("/", 1)[0].split(":", 1)[0].lstrip("*.").rstrip(".")
        if _DOMAIN_RE.match(d):
            if d not in good:
                good.append(d)
        else:
            bad.append(tok)
    return good, bad


def _build(custom: list[str]) -> str:
    data = _load_base()
    direct = data.setdefault("DirectSites", [])
    for d in custom:
        if d not in direct:
            direct.append(d)
    # Happ сравнивает LastUpdated, чтобы понять, что профиль сменился.
    data["LastUpdated"] = str(int(time.time()))
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return _PREFIX + urlsafe_b64encode(payload.encode()).decode()


def _write(deeplink: str) -> None:
    out_dir = os.path.dirname(_OUT_PATH)
    if not os.path.isdir(out_dir):
        raise RoutingError(
            f"нет каталога {out_dir} — не примонтирован общий каталог routing "
            f"(см. docker-compose.yml и panel/caddy/setup.sh)"
        )
    tmp = _OUT_PATH + ".tmp"
    try:
        # Без завершающего \n — значение уходит в HTTP-заголовок как есть.
        with open(tmp, "w") as f:
            f.write(deeplink)
        os.chmod(tmp, 0o644)  # читает системный юзер caddy, не мы
        os.replace(tmp, _OUT_PATH)  # атомарно: Caddy не увидит полфайла
    except OSError as e:
        raise RoutingError(f"не смог записать {_OUT_PATH}: {e}") from e


def publish() -> None:
    """Пересобрать и выложить профиль. Зовётся на старте бота (чтобы свежая
    база из репо доехала после деплоя) и после каждой правки списка."""
    _write(_build(custom_domains()))


def _save(domains: list[str]) -> None:
    """Сначала собираем и пишем файл, и только потом сохраняем в БД — если
    запись сорвалась, список в боте не разойдётся с тем, что реально отдаётся."""
    deeplink = _build(domains)
    if len(deeplink) > _MAX_DEEPLINK_LEN:
        raise RoutingError(
            f"профиль вырос до {len(deeplink)} байт — это уже рискованно для "
            f"HTTP-заголовка (лимит {_MAX_DEEPLINK_LEN}). Убери лишние домены."
        )
    _write(deeplink)
    db.set_setting(_SETTING_KEY, json.dumps(domains))


def add(domains: list[str]) -> tuple[list[str], list[str]]:
    """Возвращает (добавленные, пропущенные как уже имеющиеся)."""
    current = custom_domains()
    base = set(base_direct())
    added, skipped = [], []
    for d in domains:
        if d in current or d in base:
            skipped.append(d)
        else:
            current.append(d)
            added.append(d)
    if added:
        _save(current)
    return added, skipped


def remove(domain: str) -> bool:
    current = custom_domains()
    if domain not in current:
        return False
    current.remove(domain)
    _save(current)
    return True
