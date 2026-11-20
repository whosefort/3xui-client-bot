#!/usr/bin/env python3
"""Provision one node on an isolated Marzban panel."""

import argparse
import getpass
import ipaddress
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Panel:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.opener = build_opener(NoRedirect())
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def request(self, path, method="GET", body=None, content_type="application/json"):
        headers = {**self.headers, "Content-Type": content_type}
        request = Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise RuntimeError(f"Marzban {method} {path}: HTTP {error.code}") from None


def check_isolation(panel, expected_ip=None):
    if panel.request("/api/users?limit=1")["users"]:
        raise RuntimeError("Выделенная панель должна быть без пользователей.")
    nodes = panel.request("/api/nodes")
    if expected_ip is None:
        if nodes:
            raise RuntimeError("Выделенная панель должна быть без нод.")
        return False
    if len(nodes) != 1 or str(nodes[0]["address"]) != expected_ip:
        raise RuntimeError("Панель должна содержать ровно одну ноду с ожидаемым IP.")
    return nodes[0]["status"] == "connected"


def get_inbound(panel):
    inbounds = panel.request("/api/core/config")["inbounds"]
    if len(inbounds) != 1 or inbounds[0].get("protocol") != "vless":
        raise RuntimeError("На выделенной панели должен быть ровно один инбаунд, VLESS.")
    inbound = inbounds[0]
    if inbound.get("settings", {}).get("clients"):
        raise RuntimeError("Список clients в конфиге выделенного инбаунда должен быть пуст.")
    stream = inbound.get("streamSettings", {})
    if stream.get("security") != "reality" or stream.get("network", "tcp") not in ("tcp", "raw"):
        raise RuntimeError("Автосетап ожидает VLESS/TCP с REALITY.")
    if inbound["port"] != 443:
        raise RuntimeError("Проверка туннеля в bootstrap рассчитана на порт 443.")
    reality = stream.get("realitySettings", {})
    if not ((reality.get("target") or reality.get("dest")) and reality.get("serverNames")):
        raise RuntimeError("У REALITY-инбаунда должна быть настроена пара dest/SNI.")
    return inbound["tag"]


def issue_link(panel, username, inbound, node_ip):
    user_id = str(uuid4())
    body = {
        "username": username,
        "status": "active",
        "inbounds": {"vless": [inbound]},
        "proxies": {"vless": {"id": user_id, "flow": "xtls-rprx-vision"}},
    }
    created = False
    try:
        panel.request("/api/user", "POST", json.dumps(body).encode())
        created = True
        user = panel.request(f"/api/user/{username}")
        for link in user.get("links", []):
            parsed = urlsplit(link)
            if parsed.scheme != "vless" or parsed.hostname != node_ip or parsed.port != 443:
                continue
            params = parse_qs(parsed.query, keep_blank_values=True)
            if (parsed.username == user_id and params.get("security") == ["reality"]
                    and params.get("flow") == ["xtls-rprx-vision"]
                    and params.get("sni", [""])[0] and params.get("pbk", [""])[0]
                    and "sid" in params):
                return link
        raise RuntimeError("Панель не вернула корректную REALITY-ссылку на выделенную ноду.")
    except Exception:
        if created:
            panel.request(f"/api/user/{username}", "DELETE")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", required=True, help="HTTPS URL отдельной пустой панели")
    parser.add_argument("--node-ip", required=True, help="Публичный IP нового VPS")
    parser.add_argument("--panel-ip", required=True, help="IP панели для входящих правил UFW")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--username", help="Создать одного клиента с этим именем")
    mode.add_argument("--node-only", action="store_true", help="Подготовить ноду для выдачи доступа через бота")
    parser.add_argument("--xray-version", default="v26.11.9")
    args = parser.parse_args()

    if os.geteuid() != 0:
        parser.error("Запускай под root на новом VPS.")
    if platform.machine() != "x86_64":
        parser.error("Этот bootstrap устанавливает Xray для Linux x86_64.")
    url = urlsplit(args.panel)
    if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
        parser.error("--panel должен быть HTTPS URL без учётных данных, query или fragment.")
    node_ip = str(ipaddress.IPv4Address(args.node_ip))
    panel_ip = str(ipaddress.IPv4Address(args.panel_ip))
    if node_ip == panel_ip:
        parser.error("У панели и ноды должны быть разные IP.")
    if args.username and not re.fullmatch(r"[a-zA-Z0-9_]{3,32}", args.username):
        parser.error("Имя клиента должно иметь 3–32 латинских буквы, цифры или подчёркивания.")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.xray_version):
        parser.error("--xray-version должен быть тегом вида v26.11.9.")

    node_image = Path(__file__).with_name("MARZBAN_NODE_IMAGE").read_text().strip()
    if not re.fullmatch(r"gozargah/marzban-node:[\w.-]+@sha256:[a-f0-9]{64}", node_image):
        parser.error("В MARZBAN_NODE_IMAGE должен быть закреплён образ ноды с digest.")

    admin = input("Логин админа выделенной панели: ").strip()
    password = getpass.getpass("Пароль админа выделенной панели: ")
    if not admin or not password:
        parser.error("Логин и пароль обязательны.")
    panel = Panel(args.panel)
    token = panel.request(
        "/api/admin/token", "POST", urlencode({"username": admin, "password": password}).encode(),
        "application/x-www-form-urlencoded",
    )["access_token"]
    panel.headers["Authorization"] = "Bearer " + token
    check_isolation(panel)
    inbound = get_inbound(panel)

    env = {
        **os.environ,
        "PANEL_URL": args.panel,
        "PANEL_USER": admin,
        "PANEL_PASS": password,
        "PANEL_IP": panel_ip,
        "EXPECTED_NODE_IP": node_ip,
        "REQUIRE_TUNNEL_VERIFY": "1",
        "NODE_NAME": f"dedicated-{args.username or node_ip.replace('.', '-')}",
        "SERVICE_PORT": "62050",
        "XRAY_API_PORT": "62051",
        "NODE_IMAGE": node_image,
        "NODE_IMAGE_CHANNEL": "stable",
        "INBOUND_PORTS": "443",
        "XRAY_VERSION": args.xray_version,
    }
    env.pop("BOT_SSH_PUBKEY", None)
    subprocess.run(
        ["bash", str(Path(__file__).with_name("bootstrap.sh"))],
        env=env, stdin=subprocess.DEVNULL, check=True,
    )
    del env["PANEL_PASS"], password

    for _ in range(12):
        if check_isolation(panel, node_ip):
            break
        time.sleep(5)
    else:
        raise RuntimeError("Нода не перешла в connected, доступ не выдан.")

    if get_inbound(panel) != inbound:
        raise RuntimeError("REALITY-инбаунд изменился во время установки.")
    if args.node_only:
        print(f"Нода проверена. Укажите PRIVATE_MARZBAN_INBOUND={inbound} в настройках бота.")
    else:
        print(issue_link(panel, args.username, inbound, node_ip))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Автосетап остановлен: {exc}") from None
