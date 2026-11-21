"""Native 3x-ui nodes, outbound probes and infrastructure presets."""
from __future__ import annotations

import hashlib
import html
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import backup, db, runtime
from ..config import config
from ..panels.base import PanelError
from ..panels import xui_infra as infra

router = Router(name="xui_infra")
router.callback_query.filter(F.from_user.id.in_(config.admin_ids), F.message.chat.type == "private")


def _key(tag: str) -> str:
    return hashlib.blake2s(tag.encode(), digest_size=8).hexdigest()


async def _screen(message: Message, text: str, rows: list[tuple[str, str]]) -> None:
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title[:64], callback_data=callback)] for title, callback in rows
    ])
    await message.edit_text(text, reply_markup=markup)


async def _nodes(message: Message, key: str) -> None:
    name, panel = runtime.xui_panels[key]
    nodes = await infra.list_nodes(panel)
    rows = [(f"{n.get('name') or n['id']} · {n.get('status') or '—'}", f"xi:node:{key}:{n['id']}")
            for n in nodes[:30]]
    if any(other != key and p.node_token for other, (_, p) in runtime.xui_panels.items()):
        rows.append(("➕ Подключить зарегистрированную панель", f"xi:peers:{key}"))
    rows.append(("← Панель", f"xu:p:{key}:0"))
    await _screen(message, f"<b>{html.escape(name)} · Ноды</b>\nПодключено: {len(nodes)}", rows)


async def _node(message: Message, key: str, node: dict) -> None:
    node_id = node["id"]
    status = html.escape(str(node.get("status") or "неизвестно"))
    name = html.escape(str(node.get("name") or node_id))
    active = node.get("enable")
    await _screen(message, f"<b>{name}</b> · {status}\n"
                  f"Задержка: {html.escape(str(node.get('latencyMs') or '—'))} мс\n"
                  f"Клиентов: {html.escape(str(node.get('clientCount') or 0))}", [
                      ("Проверить", f"xi:probe:{key}:{node_id}"),
                      ("Выключить" if active else "Включить", f"xi:nt:{key}:{node_id}:{int(not active)}"),
                      ("Отвязать ноду", f"xi:detach:{key}:{node_id}"),
                      ("← Ноды", f"xi:nodes:{key}"),
                  ])


@router.callback_query(F.data.startswith("xi:"))
async def infrastructure(query: CallbackQuery) -> None:
    if not isinstance(query.message, Message):
        await query.answer()
        return
    await query.answer()
    message = query.message
    try:
        _, action, key, *args = query.data.split(":")
        if key not in runtime.xui_panels:
            raise PanelError("Панель больше не подключена.")
        name, panel = runtime.xui_panels[key]
        title = html.escape(name)
        back = ("← Панель", f"xu:p:{key}:0")
        if action == "backup":
            last = db.get_setting(f"xui_backup_at:{key}", "")
            date = (datetime.fromtimestamp(int(last), ZoneInfo("Europe/Moscow"))
                    .strftime("%d.%m.%Y %H:%M МСК") if last.isdigit() else "нет")
            await _screen(message, f"<b>{title} · Резервная копия</b>\n"
                          f"Последняя успешная: {date}\n"
                          "Снимок базы сохраняется в R2 с шифрованием age.",
                          [("Сделать копию", f"xi:savebackup:{key}"), back])
        elif action == "savebackup":
            await message.answer(html.escape(await backup.run_xui_backup(key)))
        elif action == "nodes":
            await _nodes(message, key)
        elif action in ("node", "probe", "nt", "na"):
            node_id = int(args[0])
            if action == "probe":
                node = await infra.probe_node(panel, node_id)
            elif action == "na":
                node = await infra.enable_node(panel, node_id, args[1] == "1")
            else:
                node = next((n for n in await infra.list_nodes(panel) if n["id"] == node_id), None)
                if not node:
                    raise PanelError("Нода больше не существует.")
                if action == "nt":
                    what = "Включить" if args[1] == "1" else "Выключить"
                    await _screen(message, f"{what} ноду {html.escape(str(node.get('name') or node_id))}?",
                                  [("Подтвердить", f"xi:na:{key}:{node_id}:{args[1]}"),
                                   ("Отмена", f"xi:node:{key}:{node_id}")])
                    return
            if not node.get("id"):
                raise PanelError("3x-ui не вернула состояние ноды.")
            await _node(message, key, node)
        elif action in ("detach", "remove"):
            node_id = int(args[0])
            if action == "detach":
                node = next((n for n in await infra.list_nodes(panel) if n["id"] == node_id), None)
                if node is None:
                    raise PanelError("Нода уже отвязана.")
                await _screen(message, f"Отвязать <b>{html.escape(str(node.get('name') or node_id))}</b>? "
                              "Привязки этой ноды в выбранной панели могут быть удалены.",
                              [("Отвязать", f"xi:remove:{key}:{node_id}"),
                               ("Отмена", f"xi:node:{key}:{node_id}")])
            else:
                await infra.remove_node(panel, node_id)
                await _nodes(message, key)
        elif action in ("it", "ia"):
            inbound_id, raw_enabled = args
            enabled = raw_enabled == "1"
            ident = int(inbound_id)
            back = ("← Вход", f"xu:i:{key}:{ident}:0")
            if action == "it":
                what = "Включить" if enabled else "Выключить"
                await _screen(message, f"{what} вход на <b>{title}</b>? "
                              "Изменение затронет всех его клиентов.",
                              [("Подтвердить", f"xi:ia:{key}:{ident}:{int(enabled)}"), back])
            else:
                await infra.enable_inbound(panel, ident, enabled)
                await _screen(message, "Вход включён." if enabled else "Вход выключен.", [back])
        elif action == "peers":
            candidates = [(n, f"xi:peer:{key}:{k}") for k, (n, p) in runtime.xui_panels.items()
                          if k != key and p.node_token]
            await _screen(message, "Выберите панель, которую нужно зарегистрировать как ноду.",
                          [*candidates, ("← Ноды", f"xi:nodes:{key}")])
        elif action in ("peer", "attach"):
            other_key = args[0]
            if other_key == key or other_key not in runtime.xui_panels:
                raise PanelError("Панель выхода больше не доступна.")
            other_name, other = runtime.xui_panels[other_key]
            if action == "peer":
                await _screen(message, f"Подключить <b>{html.escape(other_name)}</b> к <b>{title}</b> "
                              "как штатную ноду 3x-ui? Панель может синхронизировать настройки ноды.",
                              [("Подключить", f"xi:attach:{key}:{other_key}"),
                               ("Отмена", f"xi:peers:{key}")])
            else:
                await _node(message, key, await infra.add_node(panel, other, other_name))
        elif action == "out":
            template, _ = await panel.xray_template()
            outs = template.get("outbounds", [])
            rows = [(f"{o['tag']} · {o.get('protocol')}", f"xi:test:{key}:{_key(o['tag'])}")
                    for o in outs[:35] if o.get("tag") and o.get("protocol") not in ("blackhole", "dns")]
            rows += [("➕ Пресеты выхода", f"xi:presets:{key}:out"), back]
            await _screen(message, f"<b>{title} · Исходящие</b>\n"
                          "Выберите выход для HTTP-проверки с сервера.", rows)
        elif action == "test":
            template, _ = await panel.xray_template()
            tags = [o["tag"] for o in template.get("outbounds", []) if o.get("tag")]
            matches = [tag for tag in tags if _key(tag) == args[0]]
            if len(matches) != 1:
                raise PanelError("Выход изменился. Откройте список заново.")
            result = await infra.test_outbound(panel, matches[0])
            values = []
            for field, label in (("httpStatus", "HTTP"), ("statusCode", "HTTP"), ("httpCode", "HTTP"),
                                 ("delay", "Задержка, мс"), ("success", "Успех"), ("ok", "Успех")):
                if field in result and isinstance(result[field], (str, int, bool)):
                    values.append(f"{label}: {html.escape(str(result[field]))}")
            if result.get("error"):
                values.append("3x-ui сообщила об ошибке проверки.")
            await _screen(message, f"<b>{html.escape(matches[0])}</b>\n" +
                          ("\n".join(values) or "Панель не вернула измерений."),
                          [("← Исходящие", f"xi:out:{key}")])
        elif action in ("reality", "rpick", "rset"):
            ident = int(args[0])
            back = ("← Вход", f"xu:i:{key}:{ident}:0")
            if action == "reality":
                aids = {"auto": "Авто · первая рабочая цель",
                        **{k: v for k, v in infra.REALITY_PRESETS.items()}}
                rows = [(label, f"xi:rpick:{key}:{ident}:{k}") for k, label in aids.items()]
                await _screen(message, "Выберите пару dest и SNI для этого входа.", [*rows, back])
            elif action == "rpick":
                preset = args[1]
                await _screen(message, "Изменить dest и SNI существующего входа? "
                              "Клиентам этого входа потребуется обновить подписку.",
                              [("Применить", f"xi:rset:{key}:{ident}:{preset}"), back])
            else:
                host = await infra.set_reality_target(panel, ident, args[1])
                await _screen(message, f"Цель и SNI сохранены: <code>{html.escape(host)}</code>.", [back])
        elif action == "presets":
            kind = args[0]
            if kind == "in":
                labels = {"auto": "Авто · первая рабочая цель",
                          **{k: v for k, v in infra.REALITY_PRESETS.items()}}
            elif kind == "out":
                labels = {"ipv4": "Прямой IPv4", "direct": "Прямой", "block": "Блокировка"}
            else:
                raise PanelError("Неизвестный тип пресета.")
            rows = [(label, f"xi:preview:{key}:{kind}:{preset}") for preset, label in labels.items()]
            await _screen(message, f"<b>{title} · Пресеты</b>", [*rows, back])
        elif action in ("preview", "create"):
            kind, preset = args
            accepted = (kind == "in" and (preset == "auto" or preset in infra.REALITY_PRESETS)
                        or kind == "out" and preset in infra.OUTBOUND_PRESETS)
            if not accepted:
                raise PanelError("Пресет недоступен.")
            if action == "preview":
                text = ("Создать отдельный VLESS REALITY-вход? 3x-ui проверит цель, "
                        "выпустит ключи и выберет порт." if kind == "in" else
                        "Добавить выход? Текущие маршруты сохранятся, Xray кратковременно перезапустится.")
                await _screen(message, text, [("Создать", f"xi:create:{key}:{kind}:{preset}"),
                                              ("Отмена", f"xi:presets:{key}:{kind}")])
            elif kind == "in":
                item = await infra.add_reality_preset(panel, preset)
                await _screen(message, f"Вход создан в 3x-ui на порту <code>{item['port']}</code>. "
                              "Публичную доступность проверьте отдельно.", [
                                  ("Открыть вход", f"xu:i:{key}:{item['id']}:0"), back,
                              ])
            else:
                changed = await infra.add_outbound_preset(panel, preset)
                await _screen(message, "Выход сохранён." if changed else "Такой выход уже есть.",
                              [("← Исходящие", f"xi:out:{key}"), back])
    except PanelError as e:
        await message.answer(html.escape(str(e)))
    except (IndexError, KeyError, TypeError, ValueError):
        await message.answer("Откройте /xui и повторите операцию.")
