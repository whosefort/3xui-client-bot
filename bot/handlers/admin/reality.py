from __future__ import annotations

import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ... import keyboards as kb
from ... import node_provision, reality_admin, reality_scan, ssh_ops
from ...keyboards import (
    reality_confirm_kb,
    reality_fp_picker_kb,
    reality_menu_kb,
    reality_panic_pick_kb,
    reality_port_picker_kb,
    reality_scan_results_kb,
    reality_sids_kb,
    reality_sni_result_kb,
    reality_spx_picker_kb,
)
from .shared import AdminFSM, _servers_available, log, router


async def _reality_menu_text() -> tuple[str, bool]:
    try:
        s = await reality_admin.get_settings()
        ru_block_on = await reality_admin.get_ru_block_enabled()
    except reality_admin.RealityError as e:
        return f"❌ Не удалось получить настройки: {e}", False
    sni = ", ".join(s["server_names"]) or "—"
    spx_line = f"SpiderX: <code>{html.escape(s['spx'])}</code>" if s["spx"] else "SpiderX: не задан"
    ru_line = "Блок .ru/.su на ноде: включён" if ru_block_on else "Блок .ru/.su на ноде: выключен"
    text = (
        f"🛡 <b>Соединение (REALITY)</b>\n"
        f"Камуфляж (SNI): <code>{html.escape(sni)}</code>\n"
        f"dest: <code>{html.escape(s['dest'])}</code>\n"
        f"Порт: <b>{s['port']}</b>\n"
        f"publicKey: <code>{html.escape(s['public_key'])}</code>\n"
        f"shortId: <b>{len(s['short_ids'])}</b> шт.\n"
        f"{spx_line}\n"
        f"{ru_line}\n\n"
        f"Правки применяются сразу на весь кластер (общий конфиг), без "
        f"грейс-периода — старые ключи/shortId перестают работать немедленно. "
        f"Подписка генерируется на лету, отдельно рассылать новые ссылки не надо.\n\n"
        f"<b>Когда что менять:</b>\n"
        f"🔑 Ключи / 🆔 shortId — подозреваешь, что подписка/сервер конкретно "
        f"спалились (утечка, детект конкретным приложением-блокировщиком). "
        f"Рвёт ВСЕХ клиентов сразу — не для профилактики.\n"
        f"🔄 SNI — конкретный домен-камуфляж стал палевным (DPI начал резать "
        f"именно его у многих провайдеров) или жалобы именно на одну ноду — "
        f"тогда точечно через 🌍 Серверы → нода → «Свой SNI».\n"
        f"🕸 SpiderX — почти никогда не нужно трогать само по себе; меняй, "
        f"только если знаешь зачем (подгоняешь под реальную структуру "
        f"сайта-камуфляжа). Не панацея и не связана с банами.\n"
        f"🚨 Если не уверен, с чего начать при массовых жалобах — жми кнопку "
        f"ниже: она разом меняет ключи+shortId+SNI и проверяет туннели."
    )
    return text, ru_block_on


@router.message(F.text == kb.ADM_REALITY)
async def kb_reality(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _servers_available():
        await message.answer("Недоступно: нужен PANEL_BACKEND=marzban.")
        return
    text, ru_block_on = await _reality_menu_text()
    await message.answer(text, reply_markup=reality_menu_kb(ru_block_on))


@router.callback_query(F.data == "rl:close")
async def cb_rl_close(cb: CallbackQuery) -> None:
    await cb.message.delete()
    await cb.answer()


@router.callback_query(F.data == "rl:menu")
async def cb_rl_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, ru_block_on = await _reality_menu_text()
    await cb.message.edit_text(text, reply_markup=reality_menu_kb(ru_block_on))
    await cb.answer()


@router.callback_query(F.data == "rl:rublocktoggle")
async def cb_rl_rublocktoggle(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        current = await reality_admin.get_ru_block_enabled()
        await reality_admin.set_ru_block(not current)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_rublocktoggle failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    text, ru_block_on = await _reality_menu_text()
    await cb.message.edit_text(text, reply_markup=reality_menu_kb(ru_block_on))
    await cb.answer()


@router.callback_query(F.data == "rl:panic")
async def cb_rl_panic(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer("🔄 Проверяю варианты SNI перед ротацией…")
    validated = await reality_admin.validate_candidates(reality_admin.SUGGESTED_DOMAINS)
    if not validated:
        await cb.message.answer(
            "Ни один из обычных вариантов сейчас не прошёл проверку — "
            "отменяю, попробуй позже или смени SNI вручную сначала."
        )
        return
    await cb.message.answer(
        "🚨 <b>Экстренная ротация</b>: новые ключи + новый набор shortId + новый "
        "общий SNI — ОДНИМ действием на весь кластер. Порвёт вообще ВСЕХ "
        "клиентов сразу, без исключений. Если у нод были свои отдельные SNI "
        "(🌍 Серверы → нода) — они сбросятся на этот общий, придётся выставить "
        "заново вручную.\n\n"
        "Выбери новый SNI (уже прошли проверку):",
        reply_markup=reality_panic_pick_kb(validated),
    )


@router.callback_query(F.data.startswith("rl:panicpick:"))
async def cb_rl_panicpick(cb: CallbackQuery) -> None:
    domain = cb.data.split(":", 2)[2]
    await cb.answer()
    await cb.message.edit_text(f"🚨 Ротирую всё на <code>{html.escape(domain)}</code>…")
    try:
        await reality_admin.panic_rotate(domain)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_panicpick failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return

    lines = ["✅ Ключи/shortId/SNI обновлены. Проверяю туннели на всех нодах…"]
    await cb.message.answer("\n".join(lines))
    try:
        nodes = await node_provision.list_nodes()
    except node_provision.ProvisionError as e:
        await cb.message.answer(f"⚠️ Ротация прошла, но не смог получить список нод для проверки: {e}")
        nodes = []
    results = []
    for n in nodes:
        name, address = n.get("name", "?"), n.get("address", "")
        try:
            client = await node_provision.create_verify_client()
        except node_provision.ProvisionError as e:
            results.append(f"❌ {html.escape(name)}: не смог создать тестового клиента — {e}")
            continue
        try:
            ok = await ssh_ops.verify_reality_tunnel(address, client)
            results.append(f"{'✅' if ok else '❌'} {html.escape(name)} ({html.escape(address)})")
        except ssh_ops.SSHOpError as e:
            results.append(f"❌ {html.escape(name)} ({html.escape(address)}): {html.escape(str(e))}")
        finally:
            await node_provision.delete_verify_client(client["username"])
    await cb.message.answer("Готово:\n" + ("\n".join(results) if results else "нод нет"))
    text, ru_block_on = await _reality_menu_text()
    await cb.message.answer(text, reply_markup=reality_menu_kb(ru_block_on))


@router.callback_query(F.data == "rl:sni")
async def cb_rl_sni(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer("🔄 Проверяю варианты…")
    validated = await reality_admin.validate_candidates(reality_admin.SUGGESTED_DOMAINS)
    if not validated:
        await cb.message.answer(
            "Ни один из обычных вариантов сейчас не прошёл проверку — "
            "введи домен вручную или запусти скан.",
            reply_markup=reality_scan_results_kb([], show_scan=True),
        )
        return
    await cb.message.answer(
        "🔄 Прошли проверку прямо сейчас — выбери новый SNI-камуфляж:",
        reply_markup=reality_scan_results_kb(validated, show_scan=True),
    )


@router.callback_query(F.data == "rl:snimanual")
async def cb_rl_snimanual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_sni)
    await cb.message.answer(
        "Пришли домен текстом (например <code>dl.google.com</code>), без "
        "https:// и порта. Проверю TLS1.3/серт перед применением.\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


async def _check_and_offer(message: Message, state: FSMContext, domain: str) -> None:
    domain = domain.strip().lower()
    if not domain or " " in domain or "/" in domain:
        await message.answer("Не похоже на домен. Отменено.")
        await state.clear()
        return
    await message.answer(f"Проверяю {html.escape(domain)}…")
    result = await reality_admin.check_sni_candidate(domain)
    await state.update_data(sni_domain=domain)
    if result["ok"]:
        text = (
            f"✅ <code>{html.escape(domain)}</code> годится: TLS {result['tls_version']}, "
            f"ALPN {html.escape(result['alpn'])}, издатель серта «{html.escape(result['issuer'])}».\n"
            f"Применить как новый SNI-камуфляж?"
        )
    else:
        text = (
            f"⚠️ <code>{html.escape(domain)}</code> не прошёл проверку: {html.escape(result['reason'])}.\n"
            f"Можно всё равно применить (на свой риск — REALITY может не заработать), или выбрать другой домен."
        )
    await message.answer(text, reply_markup=reality_sni_result_kb(result["ok"]))


@router.message(AdminFSM.reality_sni, F.text)
async def reality_sni_input(message: Message, state: FSMContext) -> None:
    await _check_and_offer(message, state, message.text or "")


@router.callback_query(F.data.startswith("rl:pick:"))
async def cb_rl_pick(cb: CallbackQuery, state: FSMContext) -> None:
    domain = cb.data.split(":", 2)[2]
    await state.set_state(AdminFSM.reality_sni)
    await cb.answer()
    await _check_and_offer(cb.message, state, domain)


@router.callback_query(F.data == "rl:sniapply")
async def cb_rl_sniapply(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    domain = data.get("sni_domain")
    if not domain:
        await cb.answer("Домен потерялся, начни заново", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(f"Применяю {html.escape(domain)}…")
    try:
        await reality_admin.set_sni(domain)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("reality set_sni failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ SNI-камуфляж теперь <code>{html.escape(domain)}</code>.")


@router.callback_query(F.data == "rl:scan")
async def cb_rl_scan(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_scan)
    await cb.message.answer(
        "🔍 Пришли цель для скана — IP, домен или подсеть не крупнее /24 "
        "(например <code>1.2.3.0/24</code>). Может занять до пары минут.\n"
        "Отмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.reality_scan, F.text)
async def reality_scan_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    target = (message.text or "").strip()
    if not target:
        await message.answer("Пусто. Отменено.")
        return
    await message.answer(f"Сканирую {html.escape(target)}…")
    try:
        results = await reality_scan.scan(target)
    except reality_scan.ScanError as e:
        await message.answer(f"❌ {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("reality_scan.scan failed")
        await message.answer("❌ Ошибка при сканировании. См. логи.")
        return
    if not results:
        await message.answer("Ничего подходящего не нашёл в этой цели.")
        return
    lines = [f"• <code>{html.escape(r['domain'])}</code> ({html.escape(r['geo'] or '—')})" for r in results]
    await message.answer(
        "🔍 Нашёл кандидатов:\n" + "\n".join(lines) + "\n\nВыбери, чтобы проверить и применить:",
        reply_markup=reality_scan_results_kb([r["domain"] for r in results], show_scan=True),
    )


@router.callback_query(F.data == "rl:keys")
async def cb_rl_keys(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🔑 Перегенерировать REALITY-ключи? Все текущие клиенты потеряют "
        "соединение немедленно (без грейс-периода) и подключатся заново "
        "на следующем обновлении подписки в приложении.",
        reply_markup=reality_confirm_kb("keys"),
    )
    await cb.answer()


@router.callback_query(F.data == "rl:shortids")
async def cb_rl_shortids(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🆔 Перегенерировать shortId? Все текущие клиенты потеряют "
        "соединение немедленно и подключатся заново на следующем "
        "обновлении подписки в приложении.",
        reply_markup=reality_confirm_kb("shortids"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:go:"))
async def cb_rl_go(cb: CallbackQuery) -> None:
    action = cb.data.split(":", 2)[2]
    await cb.answer()
    try:
        if action == "keys":
            pub = await reality_admin.regenerate_keys()
            await cb.message.answer(f"✅ Новые ключи применены. publicKey: <code>{html.escape(pub)}</code>")
        elif action == "shortids":
            ids = await reality_admin.regenerate_short_ids()
            await cb.message.answer(f"✅ Новые shortId применены ({len(ids)} шт.).")
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
    except Exception:  # noqa: BLE001
        log.exception("reality action %s failed", action)
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")


async def _show_sids(message: Message) -> None:
    try:
        s = await reality_admin.get_settings()
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    ids = s["short_ids"]
    await message.answer(
        f"🆔 shortId сейчас: <b>{len(ids)}</b> шт.\n"
        f"Тапни по конкретному, чтобы убрать его один (остальные не тронет), "
        f"или добавь новый.",
        reply_markup=reality_sids_kb(ids),
    )


@router.callback_query(F.data == "rl:sids")
async def cb_rl_sids(cb: CallbackQuery) -> None:
    await _show_sids(cb.message)
    await cb.answer()


@router.callback_query(F.data == "rl:sidadd")
async def cb_rl_sidadd(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        new_id = await reality_admin.add_short_id()
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_sidadd failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ Добавлен shortId <code>{html.escape(new_id)}</code>, остальные не тронуты.")
    await _show_sids(cb.message)


@router.callback_query(F.data.startswith("rl:sidrm:"))
async def cb_rl_sidrm(cb: CallbackQuery) -> None:
    short_id = cb.data.split(":", 2)[2]
    await cb.answer()
    try:
        await reality_admin.remove_short_id(short_id)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_sidrm failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ shortId <code>{html.escape(short_id)}</code> убран.")
    await _show_sids(cb.message)


_PORT_WARNING = (
    "⚠️ Меняет порт на весь кластер сразу, но <b>UFW на уже развёрнутых "
    "нодах эта команда не трогает</b> — новый порт там нужно открыть "
    "руками (node/bootstrap.sh, INBOUND_PORTS), иначе клиенты просто не "
    "достучатся до ноды по новому порту."
)


async def _apply_port(message: Message, port: int) -> None:
    try:
        await reality_admin.set_port(port)
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("_apply_port failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await message.answer(
        f"✅ Порт теперь <b>{port}</b>. Не забудь открыть его в UFW на всех "
        f"уже развёрнутых нодах."
    )


@router.callback_query(F.data == "rl:port")
async def cb_rl_port(cb: CallbackQuery) -> None:
    await cb.message.answer(
        f"🔌 Выбери порт или впиши свой.\n\n{_PORT_WARNING}",
        reply_markup=reality_port_picker_kb(reality_admin.SUGGESTED_PORTS),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:portpick:"))
async def cb_rl_portpick(cb: CallbackQuery) -> None:
    port = int(cb.data.split(":", 2)[2])
    await cb.answer()
    await _apply_port(cb.message, port)


@router.callback_query(F.data == "rl:portmanual")
async def cb_rl_portmanual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_port)
    await cb.message.answer("Пришли порт текстом (число 1-65535).\nОтмена — любая кнопка снизу.")
    await cb.answer()


@router.message(AdminFSM.reality_port, F.text)
async def reality_port_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Не похоже на число. Отменено.")
        return
    await _apply_port(message, int(text))


async def _apply_spx(message: Message, value: str) -> None:
    try:
        await reality_admin.set_spx(value)
    except reality_admin.RealityError as e:
        await message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("_apply_spx failed")
        await message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    if value:
        await message.answer(f"✅ SpiderX теперь <code>{html.escape(value)}</code>.")
    else:
        await message.answer("✅ SpiderX сброшен на дефолт.")


@router.callback_query(F.data == "rl:spx")
async def cb_rl_spx(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🕸 Путь в фейковом запросе к сайту-камуфляжу — выбери, накинь "
        "случайный или впиши свой.",
        reply_markup=reality_spx_picker_kb(reality_admin.SPX_SUGGESTIONS),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:spxpick:"))
async def cb_rl_spxpick(cb: CallbackQuery) -> None:
    path = cb.data.split(":", 2)[2]
    await cb.answer()
    await _apply_spx(cb.message, path)


@router.callback_query(F.data == "rl:spxrandom")
async def cb_rl_spxrandom(cb: CallbackQuery) -> None:
    await cb.answer()
    await _apply_spx(cb.message, reality_admin.random_spx())


@router.callback_query(F.data == "rl:spxmanual")
async def cb_rl_spxmanual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFSM.reality_spx)
    await cb.message.answer(
        "Пришли SpiderX-путь текстом (например <code>/url</code>). Пустое "
        "сообщение (пробел) сбрасывает на дефолт.\nОтмена — любая кнопка снизу."
    )
    await cb.answer()


@router.message(AdminFSM.reality_spx, F.text)
async def reality_spx_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _apply_spx(message, (message.text or "").strip())


@router.callback_query(F.data == "rl:fp")
async def cb_rl_fp(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "🫆 Фингерпринт для ВСЕХ серверов разом (у каждого по отдельности — "
        "в карточке сервера, «🌍 Серверы»). «randomized» — свой отпечаток на "
        "каждое соединение, надёжнее одного фиксированного значения на весь флот.",
        reply_markup=reality_fp_picker_kb(reality_admin.FINGERPRINT_OPTIONS),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rl:fppick:"))
async def cb_rl_fppick(cb: CallbackQuery) -> None:
    fp = cb.data.split(":", 2)[2]
    await cb.answer()
    try:
        changed = await reality_admin.set_fingerprint_all(fp)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_fppick failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    await cb.message.answer(f"✅ Фингерпринт <code>{html.escape(fp)}</code> применён на {changed} серверах.")


@router.callback_query(F.data.startswith("rl:fragall:"))
async def cb_rl_fragall(cb: CallbackQuery) -> None:
    enabled = cb.data.split(":", 2)[2] == "on"
    await cb.answer()
    try:
        changed = await reality_admin.set_fragment_all(enabled)
    except reality_admin.RealityError as e:
        await cb.message.answer(f"❌ Не удалось: {e}")
        return
    except Exception:  # noqa: BLE001
        log.exception("cb_rl_fragall failed")
        await cb.message.answer("❌ Ошибка при обращении к панели. См. логи.")
        return
    state_word = "включён" if enabled else "выключен"
    await cb.message.answer(f"✅ Fragment {state_word} на {changed} серверах.")
