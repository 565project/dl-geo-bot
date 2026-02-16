import asyncio
import contextlib

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from shiftbot import config
from shiftbot.guards import ensure_staff_active, get_staff_or_reply
from shiftbot.live_registry import LIVE_REGISTRY
from shiftbot.models import MODE_AWAITING_LOCATION, MODE_CHOOSE_POINT, MODE_CHOOSE_ROLE, MODE_IDLE, MODE_REPORT_ISSUE
from shiftbot.opencart_client import ApiUnavailableError
from shiftbot.ping_alerts import process_ping_alerts

BTN_START_SHIFT = "🟢 Начать смену"
BTN_STOP_SHIFT = "🔴 Завершить смену"
BTN_STATUS = "📍 Статус"
BTN_EDIT_DATA = "✏️ Изменить данные"
BTN_HELP = "❓ Инструкция"
BTN_REPORT_ERROR = "🐞 Сообщить об ошибке"
BTN_RESTART = "🔄 Рестарт"
BTN_SEND_LOCATION = "📍 Отправить геопозицию"

ROLE_LABELS = {
    "baker": "Повар",
    "cashier": "Кассир",
    "both": "Кассир+Повар",
}


def active_shift_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_STOP_SHIFT, callback_data="stop_shift_now"), InlineKeyboardButton(BTN_STATUS, callback_data="show_status")]]
    )


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_START_SHIFT), KeyboardButton(BTN_STOP_SHIFT)],
            [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_EDIT_DATA)],
            [KeyboardButton(BTN_HELP), KeyboardButton(BTN_REPORT_ERROR)],
            [KeyboardButton(BTN_RESTART)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_SEND_LOCATION, request_location=True)]], resize_keyboard=True, one_time_keyboard=False)


def api_retry_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Повторить", callback_data=callback_data)]])


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Главное меню") -> None:
    target = update.effective_message
    if target:
        await target.reply_text(text, reply_markup=main_menu_keyboard())


def build_shift_handlers(session_store, staff_service, oc_client, dead_soul_detector, logger):
    TEST_PING_TASKS_KEY = "test_ping_tasks"

    def admin_chat_ids_from_context(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
        raw = context.application.bot_data.get("admin_chat_ids") if context and context.application else None
        if isinstance(raw, list):
            return [int(chat_id) for chat_id in raw if isinstance(chat_id, int) and chat_id > 0]
        if config.ADMIN_CHAT_ID > 0:
            return [config.ADMIN_CHAT_ID]
        return []

    async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        chat_ids = admin_chat_ids_from_context(context)
        if not chat_ids:
            logger.warning("ADMIN_CHAT_IDS_NOT_SET")
            return
        for chat_id in chat_ids:
            await context.bot.send_message(chat_id=chat_id, text=text)

    def reset_flow(session) -> None:
        session_store.reset_flow(session)

    def normalize_point(raw: dict) -> dict:
        return {
            "id": raw.get("id") or raw.get("point_id") or raw.get("location_id"),
            "short_name": raw.get("short_name") or raw.get("name") or "Точка",
            "address": raw.get("address") or "",
            "link_yandex": raw.get("link_yandex") or "",
            "link_2gis": raw.get("link_2gis") or "",
            "geo_lat": raw.get("geo_lat"),
            "geo_lon": raw.get("geo_lon") or raw.get("geo_lng") or raw.get("geo_long"),
            "geo_radius_m": raw.get("geo_radius_m") or raw.get("radius") or raw.get("geo_radius"),
        }

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def test_ping_task_key(user_id: int, chat_id: int) -> str:
        return f"{user_id}:{chat_id}"

    async def stop_test_ping_task(context: ContextTypes.DEFAULT_TYPE, key: str) -> bool:
        tasks = context.application.bot_data.setdefault(TEST_PING_TASKS_KEY, {})
        task = tasks.pop(key, None)
        if not task:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    def format_point_line(i: int, point: dict) -> str:
        address = (point.get("address") or point.get("link_yandex") or "адрес не указан").strip()
        return f"{i}) {point.get('short_name') or f'Точка {i}'} — {address}"

    async def sync_active_shift(session, staff_id: int) -> dict | None:
        shift = await oc_client.get_active_shift_by_staff(staff_id)
        if not isinstance(shift, dict):
            return None
        shift_id = as_int(shift.get("shift_id") or shift.get("id"))
        if shift_id is not None:
            session.active_shift_id = shift_id
        if shift.get("role"):
            session.active_role = str(shift.get("role"))
        session.active_started_at = shift.get("started_at") or session.active_started_at
        point_id = as_int(shift.get("point_id"))
        if point_id is not None:
            session.active_point_id = point_id
        if shift.get("point_name"):
            session.active_point_name = shift.get("point_name")
        return shift

    async def show_active_shift_exists(msg, session, shift: dict) -> None:
        shift_id = as_int(shift.get("shift_id") or shift.get("id")) or session.active_shift_id or "—"
        started = shift.get("started_at") or session.active_started_at or "—"
        await msg.reply_text(
            f"У вас уже активная смена #{shift_id} (с {started}).",
            reply_markup=active_shift_keyboard(),
        )

    async def save_selected_point(msg, session, point_index: int) -> bool:
        idx = point_index - 1
        if idx < 0 or idx >= len(session.points_cache):
            await msg.reply_text("Точка не найдена. Попробуйте выбрать другую.")
            return False

        point = session.points_cache[idx]
        p_lat = point.get("geo_lat")
        p_lon = point.get("geo_lon")
        p_rad = point.get("geo_radius_m")

        try:
            p_lat = float(p_lat) if p_lat is not None else None
            p_lon = float(p_lon) if p_lon is not None else None
            p_rad = int(p_rad) if p_rad is not None else None
        except (TypeError, ValueError):
            p_lat = None
            p_lon = None

        if p_lat is None or p_lon is None:
            await msg.reply_text("Для этой точки не задана геопозиция, выберите другую")
            session_store.patch(
                session,
                selected_point_index=None,
                selected_point_id=None,
                selected_point_name=None,
                selected_point_address=None,
                selected_point_lat=None,
                selected_point_lon=None,
                selected_point_radius=None,
            )
            return False

        session_store.patch(
            session,
            selected_point_index=point_index,
            selected_point_id=as_int(point.get("id")),
            selected_point_name=point.get("short_name") or point.get("name"),
            selected_point_address=point.get("address") or point.get("link_yandex") or "",
            selected_point_lat=p_lat,
            selected_point_lon=p_lon,
            selected_point_radius=p_rad,
            mode=MODE_CHOOSE_ROLE,
        )
        return True

    async def ask_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        session = session_store.get_or_create(user.id, chat.id)
        try:
            raw_points = await oc_client.get_points()
        except ApiUnavailableError:
            await msg.reply_text("Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.", reply_markup=api_retry_keyboard("retry_points"))
            return

        points = [normalize_point(point) for point in raw_points]
        if not points:
            await msg.reply_text("Сейчас нет доступных точек. Попробуйте позже.", reply_markup=main_menu_keyboard())
            return

        session_store.patch(
            session,
            points_cache=points,
            mode=MODE_CHOOSE_POINT,
            selected_point_index=None,
            selected_point_id=None,
            selected_point_name=None,
            selected_point_address=None,
            selected_point_lat=None,
            selected_point_lon=None,
            selected_point_radius=None,
            selected_role=None,
            gate_attempt=0,
            gate_last_reason=None,
        )

        lines = "\n".join(format_point_line(i + 1, point) for i, point in enumerate(points))
        await msg.reply_text(f"Адреса, доступные для работы:\n{lines}\n")
        await msg.reply_text("Чтобы выбрать точку — отправьте номер цифрой")

    async def stop_shift_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        session = session_store.get_or_create(user.id, chat.id)
        staff = await get_staff_or_reply(update, context, staff_service, logger)
        if not staff:
            return
        staff_id = as_int(staff.get("staff_id"))

        active_shift_id = session.active_shift_id
        if not active_shift_id and staff_id is not None:
            try:
                shift = await sync_active_shift(session, staff_id)
            except ApiUnavailableError:
                await msg.reply_text("Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.", reply_markup=api_retry_keyboard("retry_stop_shift"))
                return
            if not shift or not session.active_shift_id:
                await msg.reply_text("Активной смены нет", reply_markup=main_menu_keyboard())
                return
            active_shift_id = session.active_shift_id

        try:
            result = await oc_client.shift_end({"shift_id": active_shift_id, "end_reason": "manual"})
        except ApiUnavailableError:
            await msg.reply_text("Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.", reply_markup=api_retry_keyboard("retry_stop_shift"))
            return

        if result.get("ok") is False and result.get("error"):
            await msg.reply_text(f"Не удалось завершить смену: {result['error']}")
            return

        if active_shift_id:
            LIVE_REGISTRY.remove_shift(active_shift_id)
            dead_soul_detector.remove_shift(active_shift_id)
        session_store.clear_shift_state(session)
        reset_flow(session)
        await msg.reply_text("Смена завершена", reply_markup=main_menu_keyboard())

    async def start_shift_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        session = session_store.get_or_create(user.id, chat.id)
        staff = await get_staff_or_reply(update, context, staff_service, logger)
        if not staff:
            return

        staff_id = as_int(staff.get("staff_id"))
        if staff_id is not None:
            try:
                shift = await sync_active_shift(session, staff_id)
            except ApiUnavailableError:
                await msg.reply_text("Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.")
                return
            if shift:
                await show_active_shift_exists(msg, session, shift)
                return

        if session.active_shift_id:
            await show_active_shift_exists(msg, session, {"shift_id": session.active_shift_id, "started_at": session.active_started_at})
            return

        await ask_points(update, context)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        session = session_store.get_or_create(user.id, chat.id)
        staff = await get_staff_or_reply(update, context, staff_service, logger)
        if not staff:
            return
        staff_id = as_int(staff.get("staff_id"))

        shift = None
        if not session.active_shift_id and staff_id is not None:
            try:
                shift = await sync_active_shift(session, staff_id)
            except ApiUnavailableError:
                await msg.reply_text("Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.")
                return

        if not session.active_shift_id:
            await msg.reply_text("Смена: не активна", reply_markup=main_menu_keyboard())
            return

        shift_id = session.active_shift_id
        point_name = (shift or {}).get("point_name") or session.active_point_name or "—"
        started = (shift or {}).get("started_at") or session.active_started_at or "—"
        role = (shift or {}).get("role") or session.active_role or "—"
        await msg.reply_text(
            "Смена: активна\n"
            f"• ID: {shift_id}\n"
            f"• Точка: {point_name}\n"
            f"• Роль: {ROLE_LABELS.get(role, role)}\n"
            f"• Старт: {started}",
            reply_markup=main_menu_keyboard(),
        )

    async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return
        session = session_store.get_or_create(user.id, chat.id)
        reset_flow(session)

        staff = await get_staff_or_reply(update, context, staff_service, logger)
        if not staff:
            return
        staff_id = as_int(staff.get("staff_id"))
        shift = None
        if staff_id is not None:
            try:
                shift = await sync_active_shift(session, staff_id)
            except ApiUnavailableError:
                await msg.reply_text("Сценарий сброшен. Проверка смены временно недоступна.", reply_markup=main_menu_keyboard())
                return

        if shift and session.active_shift_id:
            await msg.reply_text("Сценарий сброшен, но у вас активная смена. Завершить её?", reply_markup=active_shift_keyboard())
            return
        await msg.reply_text("Сценарий сброшен. Можно начать заново.", reply_markup=main_menu_keyboard())

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        await update.effective_message.reply_text(
            "Краткая инструкция:\n"
            "1) Нажмите «🟢 Начать смену».\n"
            "2) Отправьте номер точки цифрой.\n"
            "3) Выберите роль.\n"
            "4) Нажмите «📍 Отправить геопозицию».\n"
            "5) В Telegram выберите «Транслировать геопозицию» → 8 часов.",
            reply_markup=main_menu_keyboard(),
        )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        await show_main_menu(update, context, "Здравствуйте! Выберите действие:")

    async def cmd_test_ping_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        args = context.args or []
        if len(args) < 3:
            await msg.reply_text("Формат: /test_ping_start <shift_id> <lat> <lon> [interval_sec=60]")
            return

        shift_id = as_int(args[0])
        lat = as_float(args[1])
        lon = as_float(args[2])
        interval_sec = as_int(args[3]) if len(args) > 3 else 60
        interval_sec = interval_sec if interval_sec and interval_sec > 0 else 60
        if shift_id is None or lat is None or lon is None:
            await msg.reply_text("Некорректные параметры. Пример: /test_ping_start 123 43.23 76.91 60")
            return

        staff = await get_staff_or_reply(update, context, staff_service, logger)
        if not staff:
            return
        staff_id = as_int(staff.get("staff_id"))
        if staff_id is None:
            await msg.reply_text("Не удалось определить staff_id для теста.")
            return

        key = test_ping_task_key(user.id, chat.id)
        await stop_test_ping_task(context, key)

        async def _loop() -> None:
            while True:
                try:
                    meta = await oc_client.ping_add_meta(
                        {
                            "shift_id": shift_id,
                            "staff_id": staff_id,
                            "lat": lat,
                            "lon": lon,
                            "source": "tg",
                        }
                    )
                    logger.info(
                        "TEST_PING status=%s body=%s shift_id=%s staff_id=%s",
                        meta.get("status"),
                        meta.get("json") or meta.get("text"),
                        shift_id,
                        staff_id,
                    )
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"TEST ping_add status={meta.get('status')} body={meta.get('json') or meta.get('text')}",
                    )
                    if isinstance(meta.get("json"), dict):
                        await process_ping_alerts(
                            response=meta["json"],
                            context=context,
                            staff_chat_id=chat.id,
                            fallback_shift_id=shift_id,
                            logger=logger,
                        )
                except ApiUnavailableError as exc:
                    logger.warning("TEST_PING_UNAVAILABLE shift_id=%s staff_id=%s error=%s", shift_id, staff_id, exc)
                except Exception as exc:
                    logger.exception("TEST_PING_ERROR shift_id=%s staff_id=%s error=%s", shift_id, staff_id, exc)
                await asyncio.sleep(interval_sec)

        task = asyncio.create_task(_loop(), name=f"test-ping-{key}")
        context.application.bot_data.setdefault(TEST_PING_TASKS_KEY, {})[key] = task
        await msg.reply_text(
            f"Запущен /test_ping_start: shift_id={shift_id}, lat={lat}, lon={lon}, interval={interval_sec}s"
        )

    async def cmd_test_ping_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return
        key = test_ping_task_key(user.id, chat.id)
        stopped = await stop_test_ping_task(context, key)
        if stopped:
            await msg.reply_text("Тестовый ping-цикл остановлен.")
        else:
            await msg.reply_text("Активный тестовый ping-цикл не найден.")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        user = update.effective_user
        chat = update.effective_chat
        if not msg or not user or not chat:
            return
        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)
        text = (msg.text or "").strip()

        if text == BTN_START_SHIFT:
            await start_shift_flow(update, context)
            return
        if text == BTN_STOP_SHIFT:
            await stop_shift_flow(update, context)
            return
        if text == BTN_STATUS:
            await cmd_status(update, context)
            return
        if text == BTN_EDIT_DATA:
            await msg.reply_text("Чтобы изменить данные, напишите: ФИО и телефон. (позже сделаем мастер).", reply_markup=main_menu_keyboard())
            return
        if text == BTN_HELP:
            await cmd_help(update, context)
            return
        if text == BTN_REPORT_ERROR:
            session.mode = MODE_REPORT_ISSUE
            await msg.reply_text("Опишите проблему одним сообщением — передадим администратору.", reply_markup=main_menu_keyboard())
            return
        if text == BTN_RESTART:
            await cmd_restart(update, context)
            return

        if session.mode == MODE_REPORT_ISSUE:
            if not admin_chat_ids_from_context(context):
                await msg.reply_text("Не удалось отправить сообщение администратору. Попробуйте позже.")
                return
            await notify_admins(
                context,
                text=(
                    "🐞 Сообщение об ошибке от сотрудника\n"
                    f"User ID: {user.id}\n"
                    f"Точка: {session.active_point_name or '—'}\n"
                    f"Shift ID: {session.active_shift_id or '—'}\n"
                    f"Текст: {text}"
                ),
            )
            session.mode = MODE_IDLE
            await msg.reply_text("✅ Сообщение отправлено администратору.", reply_markup=main_menu_keyboard())
            return

        if session.mode == MODE_CHOOSE_POINT:
            if not text.isdigit():
                await msg.reply_text("Чтобы выбрать точку — отправьте номер цифрой")
                return
            point_index = int(text)
            if point_index < 1 or point_index > len(session.points_cache):
                await msg.reply_text(f"Номер вне диапазона. Введите число от 1 до {len(session.points_cache)}.")
                return
            if not await save_selected_point(msg, session, point_index):
                lines = "\n".join(format_point_line(i + 1, point) for i, point in enumerate(session.points_cache))
                await msg.reply_text(f"Адреса, доступные для работы:\n{lines}\n")
                await msg.reply_text("Чтобы выбрать точку — отправьте номер цифрой")
                return
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Кассир", callback_data="role:cashier")],
                    [InlineKeyboardButton("Повар", callback_data="role:baker")],
                    [InlineKeyboardButton("Кассир+Повар", callback_data="role:both")],
                ]
            )
            await msg.reply_text("Выберите роль:", reply_markup=keyboard)

    async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if not query or not user or not chat:
            return
        await query.answer()
        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)
        if session.mode != MODE_CHOOSE_ROLE:
            await query.message.reply_text("Сначала выберите точку.", reply_markup=main_menu_keyboard())
            return

        role = query.data.split(":", maxsplit=1)[1]
        session.selected_role = role
        session.gate_attempt = 0
        session.gate_last_reason = None
        session.mode = MODE_AWAITING_LOCATION
        address = session.selected_point_address or "адрес не указан"
        await query.message.reply_text(
            f"Вы планируете начать смену на {session.selected_point_name or '—'} ({address}) в роли {ROLE_LABELS.get(role, role)}. "
            "Чтобы начать — отправьте Live Location (8 часов)."
        )
        await query.message.reply_text(
            "В Telegram нажмите скрепку → Геопозиция → Транслировать геопозицию → 8 часов.",
            reply_markup=location_keyboard(),
        )

    async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if not query or not user or not chat:
            return
        await query.answer()
        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        data = query.data
        if data == "change_point":
            await ask_points(update, context)
            return
        if data == "send_location":
            await query.message.reply_text("Нажмите кнопку ниже и отправьте геопозицию.", reply_markup=location_keyboard())
            return
        if data == "retry_points":
            await ask_points(update, context)
            return
        if data == "retry_stop_shift" or data == "stop_shift_now":
            await stop_shift_flow(update, context)
            return
        if data == "show_status":
            await cmd_status(update, context)
            return
        if data == "report_issue":
            session = session_store.get_or_create(user.id, chat.id)
            session.mode = MODE_REPORT_ISSUE
            await query.message.reply_text("Опишите проблему одним сообщением — передадим администратору.", reply_markup=main_menu_keyboard())

    return [
        CommandHandler("start", cmd_start),
        CommandHandler("start_shift", start_shift_flow),
        CommandHandler("stop_shift", stop_shift_flow),
        CommandHandler("status", cmd_status),
        CommandHandler("restart", cmd_restart),
        CommandHandler("help", cmd_help),
        CommandHandler("test_ping_start", cmd_test_ping_start),
        CommandHandler("test_ping_stop", cmd_test_ping_stop),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
        CallbackQueryHandler(role_callback, pattern=r"^role:"),
        CallbackQueryHandler(action_callback, pattern=r"^(change_point|send_location|report_issue|retry_points|retry_stop_shift|stop_shift_now|show_status)$"),
    ]
