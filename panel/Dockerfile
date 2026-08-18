FROM gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d

# sid-fix: подписка отдавала shortId от неверного объекта (inbound вместо
# host_inbound) — ссылки уходили с "чужим" sid.
RUN python - <<'PYFIX'
from pathlib import Path
path = Path("/code/app/subscription/share.py")
old = "                    inbound[\"sid\"] = random.choice(sids)"
new = "                    host_inbound[\"sid\"] = random.choice(sids)"
source = path.read_text()
if source.count(old) != 1:
    raise SystemExit(f"Expected exactly one SID assignment, found {source.count(old)}")
path.write_text(source.replace(old, new, 1))
PYFIX

# vless-ссылки не несли encryption=none — некоторые клиенты требуют явный параметр.
RUN python - <<'PYFIX'
from pathlib import Path
path = Path("/code/app/subscription/v2ray.py")
source = path.read_text()
start = source.index("    def vless(")
old = '        payload = {\n            "security": tls,\n'
new = '        payload = {\n            "encryption": "none",\n            "security": tls,\n'
pos = source.index(old, start)
path.write_text(source[:pos] + source[pos:].replace(old, new, 1))
PYFIX

# core_health_check(): рестарт ЛОКАЛЬНОГО core мастера ничем не обёрнут. Мастер
# по архитектуре не возит трафик (порт 443 занят панелью) — его собственный
# xray-core физически не может забиндиться и падает при каждой проверке.
# Без try/except исключение обрывает всю функцию ДО цикла проверки нод — ноды
# фактически переставали получать периодическую проверку здоровья/ресинк.
# См. node/TROUBLESHOOTING.md, раздел #3.
RUN python - <<'PYFIX'
from pathlib import Path
path = Path("/code/app/jobs/0_xray_core.py")
source = path.read_text()
old = """    # main core
    if not xray.core.started:
        if not config:
            config = xray.config.include_db_users()
        xray.core.restart(config)"""
new = """    # main core
    if not xray.core.started:
        if not config:
            config = xray.config.include_db_users()
        try:
            xray.core.restart(config)
        except Exception:
            logger.warning("master local core restart failed, continuing node checks", exc_info=True)"""
if source.count(old) != 1:
    raise SystemExit(f"Expected exactly one main-core restart block, found {source.count(old)}")
path.write_text(source.replace(old, new, 1))
PYFIX

# reality-fragment: fragment_setting на хосте доходит до ссылки только для
# tls=="tls" — ветка tls=="reality" его никогда не проверяла (апстрим просто
# забыл). Без этого fragment (обход DPI по сигнатуре ClientHello) на
# REALITY-инбаундах в принципе не мог долететь до клиента через подписку.
# В файле три идентичных блока (vmess/vless/trojan) — правим только внутри
# def vless(), тем же приёмом anchor+index, что и encryption=none выше.
RUN python - <<'PYFIX'
from pathlib import Path
path = Path("/code/app/subscription/v2ray.py")
source = path.read_text()
start = source.index("    def vless(")
old = '''        elif tls == "reality":
            payload["sni"] = sni
            payload["fp"] = fp
            payload["pbk"] = pbk
            payload["sid"] = sid
            if spx:
                payload["spx"] = spx'''
new = '''        elif tls == "reality":
            payload["sni"] = sni
            payload["fp"] = fp
            payload["pbk"] = pbk
            payload["sid"] = sid
            if spx:
                payload["spx"] = spx
            if fs:
                payload["fragment"] = fs'''
pos = source.index(old, start)
path.write_text(source[:pos] + source[pos:].replace(old, new, 1))
PYFIX
