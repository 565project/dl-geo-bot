from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from shiftbot import config
from shiftbot.guards import ensure_staff_active
from shiftbot.models import MODE_AWAITING_LOCATION, MODE_CHOOSE_POINT, MODE_CHOOSE_ROLE, MODE_IDLE, MODE_REPORT_ISSUE
from shiftbot.opencart_client import ApiUnavailableError

BTN_START_SHIFT = "✅ Начать смену"
BTN_STOP_SHIFT = "🛑 Завершить смену"
BTN_EDIT_DATA = "🧾 Изменить данные"
BTN_REPORT_ERROR = "🆘 Сообщить об ошибке"
BTN_HELP = "📘 Инструкция"
BTN_RESTART = "🔄 Рестарт"
BTN_SEND_LOCATION = "📍 Отправить геопозицию"

ROLE_LABELS = {
    "baker": "Повар",
    "cashier": "Кассир",
    "both": "Кассир+Повар",
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_START_SHIFT), KeyboardButton(BTN_STOP_SHIFT)],
            [KeyboardButton(BTN_EDIT_DATA), KeyboardButton(BTN_REPORT_ERROR)],
            [KeyboardButton(BTN_HELP), KeyboardButton(BTN_RESTART)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_SEND_LOCATION, request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def api_retry_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Повторить", callback_data=callback_data)]])


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Главное меню") -> None:
    target = update.effective_message
    if target:
        await target.reply_text(text, reply_markup=main_menu_keyboard())


def build_shift_handlers(session_store, staff_service, oc_client, logger):
    def reset_flow(session) -> None:
        session_store.reset_flow(session)

    def selected_point(session) -> dict | None:
        if session.selected_point_index is None:
            return None
        idx = session.selected_point_index - 1
        if idx < 0 or idx >= len(session.points_cache):
            return None
        return session.points_cache[idx]

    def format_point_line(i: int, point: dict) -> str:
        address = (point.get("address") or "").strip()
        if not address:
            address = "адрес не указан"
            if point.get("link_yandex"):
                address = f"{address} ({point['link_yandex']})"
        short_name = point.get("short_name") or f"Точка {i}"
        return f"{i}) {short_name} — {address}"

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
            if p_lat is not None:
                p_lat = float(p_lat)
            if p_lon is not None:
                p_lon = float(p_lon)
            if p_rad is not None:
                p_rad = int(p_rad)
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
            selected_point_address=point.get("address"),
            selected_point_lat=p_lat,
            selected_point_lon=p_lon,
            selected_point_radius=p_rad,
            mode=MODE_CHOOSE_ROLE,
        )
        radius_text = f"{p_rad}м" if p_rad is not None else "не задан"
        await msg.reply_text(
            f"Выбрана точка: {session.selected_point_name or '—'}. "
            f"Координаты: {p_lat}, {p_lon}. Радиус: {radius_text}"
        )
        return True

    async def get_admin_chat_id() -> int | None:
        admin = await staff_service.get_staff_by_phone(config.ADMIN_PHONE)
        if not admin:
            return None
        chat_id = admin.get("telegram_chat_id")
        try:
            return int(chat_id) if chat_id is not None else None
        except (TypeError, ValueError):
            return None

    async def start_report_issue_mode(msg, session) -> None:
        session.mode = MODE_REPORT_ISSUE
        await msg.reply_text(
            "Опишите проблему одним сообщением — передадим администратору.",
            reply_markup=main_menu_keyboard(),
        )

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
            await msg.reply_text(
                "Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.",
                reply_markup=api_retry_keyboard("retry_points"),
            )
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
        await msg.reply_text(f"Адреса, доступные для работы:\n{lines}")
        await msg.reply_text("Чтобы выбрать точку — отправьте номер в чат цифрой (например 1).")

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        await show_main_menu(update, context, "Здравствуйте! Выберите действие в меню ниже.")

    async def start_shift_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return
        session = session_store.get_or_create(user.id, chat.id)
        if session.active_shift_id:
            await update.effective_message.reply_text("Смена уже активна. Если нужно — завершите её кнопкой «🛑 Завершить смену».", reply_markup=main_menu_keyboard())
            return
        await ask_points(update, context)

    async def stop_shift_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        session = session_store.get_or_create(user.id, chat.id)
        if not session.active_shift_id:
            await msg.reply_text("Активной смены нет.", reply_markup=main_menu_keyboard())
            return

        payload = {"shift_id": session.active_shift_id, "reason": "manual"}
        try:
            result = await oc_client.shift_end(payload)
        except ApiUnavailableError:
            await msg.reply_text(
                "Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.",
                reply_markup=api_retry_keyboard("retry_stop_shift"),
            )
            return

        if result.get("ok") is False and result.get("error"):
            await msg.reply_text(f"Не удалось завершить смену: {result['error']}")
            return

        session_store.clear_shift_state(session)
        reset_flow(session)
        await msg.reply_text("🛑 Смена завершена.", reply_markup=main_menu_keyboard())

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg:
            return

        session = session_store.get_or_create(user.id, chat.id)
        if not session.active_shift_id:
            await msg.reply_text("Смена не начата.", reply_markup=main_menu_keyboard())
            return

        point = next((p for p in session.points_cache if p.get("id") == session.active_point_id), None)
        point_name = point.get("short_name") if point else (session.active_point_name or "—")
        started = session.active_started_at or "—"
        await msg.reply_text(
            "Смена активна:\n"
            f"• Точка: {point_name}\n"
            f"• Роль: {ROLE_LABELS.get(session.active_role or '', session.active_role or '—')}\n"
            f"• Старт: {started}\n"
            f"• ID смены: {session.active_shift_id}",
            reply_markup=main_menu_keyboard(),
        )

    async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return
        session = session_store.get_or_create(user.id, chat.id)
        reset_flow(session)
        await show_main_menu(update, context, "Сценарий сброшен. Можно начать заново.")

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_staff_active(update, context, staff_service, logger):
            return
        await update.effective_message.reply_text(
            "Краткая инструкция:\n"
            "1) Нажмите «✅ Начать смену».\n"
            "2) Отправьте номер точки цифрой.\n"
            "3) Выберите роль.\n"
            "4) Нажмите «📍 Отправить геопозицию».\n"
            "5) В Telegram выберите «Транслировать геопозицию» → 8 часов.",
            reply_markup=main_menu_keyboard(),
        )

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
        if text == BTN_EDIT_DATA:
            await msg.reply_text(
                "Чтобы изменить данные, напишите: ФИО и телефон. (позже сделаем мастер).",
                reply_markup=main_menu_keyboard(),
            )
            return
        if text == BTN_REPORT_ERROR:
            await start_report_issue_mode(msg, session)
            return
        if text == BTN_HELP:
            await cmd_help(update, context)
            return
        if text == BTN_RESTART:
            await cmd_restart(update, context)
            return

        if session.mode == MODE_REPORT_ISSUE:
            admin_chat_id = await get_admin_chat_id()
            if admin_chat_id is None:
                await msg.reply_text("Не удалось отправить сообщение администратору. Попробуйте позже.")
                return

            point_name = session.active_point_name or "—"
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=(
                    "🆘 Сообщение об ошибке от сотрудника\n"
                    f"User ID: {user.id}\n"
                    f"Точка: {point_name}\n"
                    f"Shift ID: {session.active_shift_id or '—'}\n"
                    f"Текст: {text}"
                ),
            )
            session.mode = MODE_IDLE
            await msg.reply_text("✅ Сообщение отправлено администратору.", reply_markup=main_menu_keyboard())
            return

        if session.mode == MODE_CHOOSE_POINT:
            if not text.isdigit():
                await msg.reply_text("Нужна цифра: 1, 2, 3... Отправьте номер точки.")
                return

            point_index = int(text)
            if point_index < 1 or point_index > len(session.points_cache):
                await msg.reply_text(f"Номер вне диапазона. Введите число от 1 до {len(session.points_cache)}.")
                return

            if not await save_selected_point(msg, session, point_index):
                return

            title = session.selected_point_name or "Точка"

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("👨‍🍳 Повар", callback_data="role:baker")],
                    [InlineKeyboardButton("🧾 Кассир", callback_data="role:cashier")],
                    [InlineKeyboardButton("🔁 Кассир+Повар", callback_data="role:both")],
                    [InlineKeyboardButton("⭐ Администратор", callback_data="role:admin")],
                ]
            )
            await msg.reply_text(f"Вы выбрали: {title}. Теперь выберите роль:", reply_markup=keyboard)
            return

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
        if role == "admin":
            await query.message.reply_text("Роль администратора появится позже. Выберите: кассир/повар/оба.")
            return

        if session.selected_point_lat is None or session.selected_point_lon is None:
            await query.message.reply_text("Для этой точки не задана геопозиция, выберите другую")
            session.mode = MODE_CHOOSE_POINT
            return

        session.selected_role = role
        session.gate_attempt = 0
        session.gate_last_reason = None
        session.mode = MODE_AWAITING_LOCATION

        address = session.selected_point_address or "адрес не указан"
        await query.message.reply_text(
            "Вы планируете начать смену:\n"
            f"• Точка: {session.selected_point_name or '—'}\n"
            f"• Адрес: {address}\n"
            f"• Роль: {ROLE_LABELS.get(role, role)}\n"
            "Чтобы начать смену — отправьте трансляцию геопозиции.",
        )
        await query.message.reply_text(
            "Важно: выберите «Транслировать геопозицию» → 8 часов.",
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

        session = session_store.get_or_create(user.id, chat.id)
        data = query.data
        if data == "change_point":
            session_store.patch(
                session,
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
                mode=MODE_CHOOSE_POINT,
            )
            await query.message.reply_text("Хорошо, выбираем точку заново.")
            await ask_points(update, context)
            return

        if data == "send_location":
            await query.message.reply_text(
                "Нажмите кнопку ниже и отправьте геопозицию.",
                reply_markup=location_keyboard(),
            )
            return

        if data == "retry_points":
            await ask_points(update, context)
            return

        if data == "retry_stop_shift":
            await stop_shift_flow(update, context)
            return

        if data == "report_issue":
            await start_report_issue_mode(query.message, session)
            return

    async def cmd_start_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await start_shift_flow(update, context)

    async def cmd_stop_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await stop_shift_flow(update, context)

    return [
        CommandHandler("start", cmd_start),
        CommandHandler("start_shift", cmd_start_shift),
        CommandHandler("stop_shift", cmd_stop_shift),
        CommandHandler("status", cmd_status),
        CommandHandler("restart", cmd_restart),
        CommandHandler("help", cmd_help),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
        CallbackQueryHandler(role_callback, pattern=r"^role:"),
        CallbackQueryHandler(action_callback, pattern=r"^(change_point|send_location|report_issue|retry_points|retry_stop_shift)$"),
    ]
