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


async def _connect(address: str, *, user: str = "root", port: int = 22, connect_timeout: float = 15.0):
    ensure_keypair()
    try:
        return await asyncssh.connect(
            address, port=port, username=user, client_keys=[_PRIVATE_KEY_PATH],
            known_hosts=None, connect_timeout=connect_timeout,
        )
    except (asyncssh.Error, OSError) as e:
        raise SSHOpError(f"SSH до {address} не удался: {e}") from e


async def upgrade_node(address: str, xray_version: str, *, user: str = "root",
                       port: int = 22, timeout: float = 180.0) -> str:
    """Копирует содержимое node/upgrade_xray.sh на ноду через stdin и
    гоняет с нужным пином версии. Возвращает хвост вывода для отчёта
    админу. Кидает SSHOpError с текстом причины при любом сбое."""
    with open(_UPGRADE_SCRIPT) as f:
        script = f.read()

    conn = await _connect(address, user=user, port=port)
    try:
        result = await asyncio.wait_for(
            conn.run(f"XRAY_VERSION={xray_version} bash -s", input=script, check=False),
            timeout=timeout,
        )
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
        raise SSHOpError(f"SSH до {address} не удался: {e}") from e
    finally:
        conn.close()

    tail = "\n".join((result.stdout or "").strip().splitlines()[-15:])
    if result.exit_status != 0:
        err_tail = "\n".join((result.stderr or "").strip().splitlines()[-5:])
        raise SSHOpError(f"exit={result.exit_status}\n{tail}\n{err_tail}".strip())
    return tail


async def get_resources(address: str, *, user: str = "root", port: int = 22,
                        timeout: float = 20.0) -> str:
    """RAM + диск ноды по SSH — 'free -h' и 'df -h /', сырой вывод."""
    conn = await _connect(address, user=user, port=port)
    try:
        result = await asyncio.wait_for(
            conn.run("free -h; echo '---DISK---'; df -h /", check=False),
            timeout=timeout,
        )
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
        raise SSHOpError(f"SSH до {address} не удался: {e}") from e
    finally:
        conn.close()
    if result.exit_status != 0:
        raise SSHOpError(f"exit={result.exit_status}: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


async def verify_reality_tunnel(address: str, client: dict, *, user: str = "root",
                                port: int = 22, container: str = "marzban-node-marzban-node-1",
                                timeout: float = 40.0) -> bool:
    """Честная проверка REALITY прямо с ноды: запускает тестовый xray-клиент
    ВНУТРИ marzban-node контейнером (network_mode: host — сокет виден с
    хоста), гоняет через него запрос, убивает по PID на порту (не по имени
    процесса — рядом всегда крутится боевой xray, его трогать нельзя).
    `client` — словарь uuid/pbk/sid/sni/fp (см. node_provision.build_verify_client).
    Возвращает True при http=204, иначе кидает SSHOpError с деталями."""
    import json as _json

    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"port": 19999, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{"address": address, "port": 443, "users": [
                {"id": client["uuid"], "encryption": "none", "flow": "xtls-rprx-vision"}
            ]}]},
            "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                "serverName": client["sni"], "fingerprint": client.get("fp") or "chrome",
                "publicKey": client["pbk"], "shortId": client["sid"], "spiderX": "/",
            }},
        }],
    }
    remote_cfg = f"/var/lib/marzban-node/verify_client_{os.getpid()}.json"
    conn = await _connect(address, user=user, port=port)
    try:
        put = await asyncio.wait_for(
            conn.run(f"cat > {remote_cfg}", input=_json.dumps(cfg), check=False), timeout=15,
        )
        if put.exit_status != 0:
            raise SSHOpError(f"не смог записать тестовый конфиг на ноду: {(put.stderr or '').strip()}")

        script = (
            f"cd /opt/marzban-node && "
            f"docker compose exec -d marzban-node xray run -config '{remote_cfg}' && "
            f"sleep 3 && "
            f"curl --socks5-hostname 127.0.0.1:19999 -s -o /dev/null -w '%{{http_code}}' "
            f"--max-time 10 https://www.gstatic.com/generate_204; "
            f"PID=$(ss -tlnp 2>/dev/null | grep '127.0.0.1:19999' | grep -oE 'pid=[0-9]+' | cut -d= -f2); "
            f"[ -n \"$PID\" ] && kill \"$PID\" 2>/dev/null; "
            f"rm -f '{remote_cfg}'"
        )
        result = await asyncio.wait_for(conn.run(script, check=False), timeout=timeout)
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
        raise SSHOpError(f"SSH до {address} не удался: {e}") from e
    finally:
        conn.close()

    code = (result.stdout or "").strip()
    if code != "204":
        raise SSHOpError(f"туннель не работает (http={code or 'нет ответа'})")
    return True
