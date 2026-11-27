"""Native 3x-ui nodes, outbound probes and infrastructure presets."""
from __future__ import annotations

import hashlib
import html
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import backup, db, runtime
from ..config import config
from ..panels.base import PanelError
from ..panels.xui_routes import exit_outbound, outbound_tags, set_client_route
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
    rows.append(("➕ Подключить ноду к мастеру", f"xi:peers:{key}"))
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
async def infrastructure(query: CallbackQuery, state: FSMContext) -> None:
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
            await _screen(message, "Выберите будущую ноду. Здесь видны другие зарегистрированные панели с токеном ноды.",
                          [*candidates, ("Зарегистрировать готовую 3x-ui", "xu:register"), ("← Ноды", f"xi:nodes:{key}")])
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
            rows = [(f"{o['tag']} · {o.get('protocol')}", f"xi:outinfo:{key}:{_key(o['tag'])}")
                    for o in outs[:35] if o.get("tag")]
            rows += [("➕ Выход через другую панель", f"xi:relay:{key}"), ("➕ Пресеты выхода", f"xi:presets:{key}:out"), back]
            await _screen(message, f"<b>{title} · Исходящие</b>\n"
                          "Откройте выход, чтобы проверить его и посмотреть назначения.", rows)
        elif action == "relay":
            rows = [(name, f"xi:relaypick:{key}:{other}") for other, (name, peer)
                    in runtime.xui_panels.items() if other != key and peer.exit_client]
            await _screen(message, "Выберите панель с выделенным клиентом выхода VLESS REALITY.",
                          [*rows, ("← Аутбаунды", f"xi:out:{key}")])
        elif action in ("relaypick", "relayadd"):
            peer_key = args[0]
            if peer_key == key or peer_key not in runtime.xui_panels:
                raise PanelError("Выходная панель недоступна.")
            peer_name, peer = runtime.xui_panels[peer_key]
            cancel = ("← Аутбаунды", f"xi:out:{key}")
            if action == "relaypick":
                await _screen(message, f"Создать на {title} выход через {html.escape(peer_name)}? "
                              "Xray кратковременно перезапустится. Назначить клиентов можно после создания.",
                              [("Создать выход", f"xi:relayadd:{key}:{peer_key}"), cancel])
            else:
                outbound = await exit_outbound(peer, peer_key)
                changed = await infra.add_outbound(panel, outbound)
                await _screen(message, "Выход создан." if changed else "Выход уже настроен.",
                              [("Открыть выход", f"xi:outinfo:{key}:{_key(outbound['tag'])}"), cancel])
        elif action in ("outinfo", "outdel", "outconfirm"):
            template, _ = await panel.xray_template()
            matches = [o for o in template.get("outbounds", []) if o.get("tag") and _key(o["tag"]) == args[0]]
            if len(matches) != 1:
                raise PanelError("Выход изменился. Откройте список заново.")
            outbound = matches[0]
            tag = outbound["tag"]
            outbound_back = ("← Исходящие", f"xi:out:{key}")
            if action == "outconfirm":
                await infra.remove_unused_outbound(panel, tag)
                await _screen(message, "Выход удалён.", [outbound_back])
                return
            if action == "outdel":
                await _screen(message, f"Удалить <b>{html.escape(tag)}</b>? Xray кратковременно перезапустится.",
                              [("Подтвердить", f"xi:outconfirm:{key}:{args[0]}"), outbound_back])
                return
            rules = infra.outbound_assignments(template, tag)
            assigned = []
            for rule in rules[:12]:
                subjects = ", ".join(str(x) for x in (rule.get("user") or rule.get("inboundTag") or []))
                assigned.append("• " + html.escape(subjects[:100] or "Общее правило"))
            rows = []
            if tag in outbound_tags(template):
                rows.append(("Назначить клиенту", f"xi:outpick:{key}:{args[0]}"))
            if outbound.get("protocol") not in ("blackhole", "dns"):
                rows.insert(0, ("Проверить HTTP", f"xi:test:{key}:{args[0]}"))
            if not rules and template.get("outbounds", [])[0]["tag"] != tag:
                rows.append(("Удалить неиспользуемый выход", f"xi:outdel:{key}:{args[0]}"))
            await _screen(message, f"<b>{html.escape(tag)}</b>\nПравил: {len(rules)}\n" +
                          "\n".join(assigned), [*rows, outbound_back])
        elif action == "outpick":
            template, _ = await panel.xray_template()
            matches = [tag for tag in outbound_tags(template) if _key(tag) == args[0]]
            if len(matches) != 1:
                raise PanelError("Выход изменился. Откройте список заново.")
            await state.update_data(xui_outbound=[message.message_id, key, matches[0]],
                                    xui_outbound_confirm=None)
            inbounds = await panel.list_inbounds()
            rows = [(f"{row.get('remark') or row['tag']} · {row['port']}", f"xi:outusers:{key}:{row['id']}:0")
                    for row in inbounds if row.get("tag") and row["settings"].get("clients")]
            await _screen(message, f"Выберите вход для назначения <b>{html.escape(matches[0])}</b>.",
                          [*rows[:35], ("← Выход", f"xi:outinfo:{key}:{args[0]}")])
        elif action in ("outusers", "outuser", "outapply"):
            context = (await state.get_data()).get("xui_outbound")
            if not context or context[:2] != [message.message_id, key]:
                raise PanelError("Выбор выхода устарел. Откройте карточку выхода заново.")
            tag = context[2]
            inbound_id = int(args[0])
            inbound = next((row for row in await panel.list_inbounds() if row["id"] == inbound_id), None)
            if inbound is None:
                raise PanelError("Вход больше не существует.")
            emails = [client["email"] for client in inbound["settings"].get("clients", []) if client.get("email")]
            back = ("← Входы", f"xi:outpick:{key}:{_key(tag)}")
            if action == "outusers":
                page = min(max(0, int(args[1])), max(0, (len(emails) - 1) // 15))
                rows = [(email, f"xi:outuser:{key}:{inbound_id}:{_key(email)}")
                        for email in emails[page * 15:(page + 1) * 15]]
                if page:
                    rows.append(("← Предыдущие", f"xi:outusers:{key}:{inbound_id}:{page - 1}"))
                if (page + 1) * 15 < len(emails):
                    rows.append(("Следующие →", f"xi:outusers:{key}:{inbound_id}:{page + 1}"))
                await _screen(message, f"Выберите клиента для <b>{html.escape(tag)}</b>.", [*rows, back])
                return
            matches = [email for email in emails if _key(email) == args[1]]
            if len(matches) != 1:
                raise PanelError("Клиент больше не привязан к этому входу.")
            email = matches[0]
            confirmation = [message.message_id, key, tag, inbound_id, email]
            if action == "outuser":
                await state.update_data(xui_outbound_confirm=confirmation)
                await _screen(message, f"Назначить <b>{html.escape(tag)}</b> клиенту "
                              f"<b>{html.escape(email)}</b>? Xray кратковременно перезапустится.",
                              [("Подтвердить", f"xi:outapply:{key}:{inbound_id}:{args[1]}"), back])
                return
            if (await state.get_data()).get("xui_outbound_confirm") != confirmation:
                raise PanelError("Подтверждение устарело. Выберите клиента заново.")
            await state.update_data(xui_outbound_confirm=None)
            changed = await set_client_route(panel, inbound_id, email, tag)
            await _screen(message, "Выход назначен и проверен." if changed else "Выход уже назначен.",
                          [("← Выход", f"xi:outinfo:{key}:{_key(tag)}")])
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
