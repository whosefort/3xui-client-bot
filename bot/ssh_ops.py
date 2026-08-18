"""SSH-доступ бота к нодам — единственная цель: удалённо гонять
node/upgrade_xray.sh, не заходя руками по SSH на каждую ноду.

Отдельный ed25519-ключ, не тот, что у оператора. Приватная часть живёт в
data/ рядом с bot.db (том, переживает пересборки и не улетает в git).
Публичную часть бот сам прописывает в authorized_keys НОВЫХ нод при
бутстрапе (см. node/bootstrap_token.sh, node/bootstrap.sh) — уже
существующим нодам ключ придётся один раз добавить руками
(ensure_keypair() всегда возвращает публичную часть для этого).

Хосты не пинуются (known_hosts=None) — тот же implicit-trust, что и у
curl-piped bootstrap-скриптов на свежих VPS: доверие уже оказано на этапе
аренды сервера у хостера, отдельный TOFU поверх этого номинальный."""
from __future__ import annotations

import asyncio
import logging
import os

import asyncssh

log = logging.getLogger("ssh_ops")

_KEY_DIR = "data"
_PRIVATE_KEY_PATH = os.path.join(_KEY_DIR, "node_ssh_ed25519")
_PUBLIC_KEY_PATH = _PRIVATE_KEY_PATH + ".pub"

_UPGRADE_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node", "upgrade_xray.sh"
)


class SSHOpError(Exception):
    pass


def ensure_keypair() -> str:
    """Генерирует ключ при первом обращении. Возвращает публичную часть
    (одна строка, формат authorized_keys)."""
    os.makedirs(_KEY_DIR, exist_ok=True)
    if not os.path.exists(_PRIVATE_KEY_PATH):
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(_PRIVATE_KEY_PATH)
        os.chmod(_PRIVATE_KEY_PATH, 0o600)
        key.write_public_key(_PUBLIC_KEY_PATH)
        log.info("Сгенерировал новый SSH-ключ бота для апгрейда нод")
    with open(_PUBLIC_KEY_PATH) as f:
        return f.read().strip()


async def upgrade_node(address: str, xray_version: str, *, user: str = "root",
                       port: int = 22, timeout: float = 180.0) -> str:
    """Копирует содержимое node/upgrade_xray.sh на ноду через stdin и
    гоняет с нужным пином версии. Возвращает хвост вывода для отчёта
    админу. Кидает SSHOpError с текстом причины при любом сбое."""
    ensure_keypair()
    with open(_UPGRADE_SCRIPT) as f:
        script = f.read()

    try:
        async with asyncssh.connect(
            address, port=port, username=user, client_keys=[_PRIVATE_KEY_PATH],
            known_hosts=None, connect_timeout=15,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(f"XRAY_VERSION={xray_version} bash -s", input=script, check=False),
                timeout=timeout,
            )
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
        raise SSHOpError(f"SSH до {address} не удался: {e}") from e

    tail = "\n".join((result.stdout or "").strip().splitlines()[-15:])
    if result.exit_status != 0:
        err_tail = "\n".join((result.stderr or "").strip().splitlines()[-5:])
        raise SSHOpError(f"exit={result.exit_status}\n{tail}\n{err_tail}".strip())
    return tail
