import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from shiftbot import config
from shiftbot.geo import haversine_m
from shiftbot.guards import ensure_staff_active
from shiftbot.handlers_shift import main_menu_keyboard
from shiftbot.models import MODE_AWAITING_LOCATION, MODE_IDLE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN


def build_location_handlers(session_store, staff_service, oc_client, logger):
    def selected_point(session) -> dict | None:
        if session.selected_point_index is None:
            return None
        idx = session.selected_point_index - 1
        if idx < 0 or idx >= len(session.points_cache):
            return None
        return session.points_cache[idx]

    def as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def retry_inline_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📍 Отправить геопозицию", callback_data="send_location")],
                [InlineKeyboardButton("🔁 Сменить точку", callback_data="change_point")],
            ]
        )

    def out_alert_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🆘 Сообщить об ошибке", callback_data="report_issue")],
                [InlineKeyboardButton("🔁 Сменить точку", callback_data="change_point")],
            ]
        )

    async def maybe_notify_admin(context, session, staff, dist_m: float, radius_m: float) -> None:
        now = time.time()
        if (now - session.last_admin_alert_at) < config.OUT_COOLDOWN_SEC:
            return
        session.last_admin_alert_at = now

        admin = await staff_service.get_staff_by_phone(config.ADMIN_PHONE)
        if not admin:
            logger.warning("ADMIN_NOT_FOUND phone=%s", config.ADMIN_PHONE)
            return

        admin_chat_id = admin.get("telegram_chat_id")
        if not admin_chat_id:
            logger.warning("ADMIN_CHAT_ID_EMPTY phone=%s", config.ADMIN_PHONE)
            return

        full_name = (
            staff.get("full_name")
            or staff.get("name")
            or staff.get("fio")
            or f"user_id={session.user_id}"
        )

        await context.bot.send_message(
            chat_id=int(admin_chat_id),
            text=(
                "🚨 Геоконтроль: сотрудник вне зоны 3 раза подряд\n"
                f"ФИО: {full_name}\n"
                f"Точка: {session.active_point_name or '—'}\n"
                f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Дистанция: ~{dist_m:.0f} м (радиус {radius_m:.0f} м)\n"
                f"Shift ID: {session.active_shift_id or '—'}\n"
                "Рекомендация: связаться с руководителем точки."
            ),
        )

    async def handle_active_shift_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE, session, location) -> None:
        if session.active_point_lat is None or session.active_point_lon is None:
            return

        now = time.time()
        lat = location.latitude
        lon = location.longitude
        accuracy = getattr(location, "horizontal_accuracy", None)

        session.last_ping_ts = now
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None

        if accuracy is None or accuracy > config.ACCURACY_MAX_M:
            session.last_status = STATUS_UNKNOWN
            session.last_distance_m = None
            if (now - session.last_out_warn_at) >= config.OUT_COOLDOWN_SEC:
                session.last_out_warn_at = now
                acc_text = f"{accuracy:.0f}" if accuracy is not None else "неизвестна"
                await update.message.reply_text(
                    "⚠️ Слабый GPS сигнал "
                    f"(точность {acc_text} м). Проверьте GPS/выйдите к окну/на улицу."
                )
            return

        dist_m = haversine_m(lat, lon, session.active_point_lat, session.active_point_lon)
        radius_m = session.active_point_radius or float(config.DEFAULT_RADIUS_M)

        session.last_distance_m = dist_m
        session.last_valid_ping_ts = now

        if dist_m <= radius_m:
            session.last_status = STATUS_IN
            if session.consecutive_out_count > 0:
                session.consecutive_out_count = 0
                await update.message.reply_text("✅ Вы снова в рабочей зоне. Спасибо!")
            return

        session.last_status = STATUS_OUT
        session.consecutive_out_count = min(session.consecutive_out_count + 1, config.OUT_LIMIT)

        if session.consecutive_out_count < config.OUT_LIMIT:
            await update.message.reply_text(
                "⚠️ Вы вне рабочего радиуса точки "
                f"(≈{dist_m:.0f} м, допустимо {radius_m:.0f} м).\n"
                "Если это ошибка — включите GPS и продолжайте трансляцию."
            )
            return

        await update.message.reply_text(
            "❗️Вы 3 раза подряд вне рабочего радиуса.\n"
            "Вернитесь на точку или сообщите об ошибке администратору.",
            reply_markup=out_alert_keyboard(),
        )

        try:
            staff = await staff_service.get_staff(session.user_id)
            await maybe_notify_admin(context, session, staff or {}, dist_m, radius_m)
        except Exception:
            logger.exception("ADMIN_NOTIFY_FAILED user=%s", session.user_id)

    async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.location:
            return

        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return

        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)

        if session.mode != MODE_AWAITING_LOCATION and session.active_shift_id:
            await handle_active_shift_monitoring(update, context, session, update.message.location)
            return

        if session.mode != MODE_AWAITING_LOCATION:
            return

        point = selected_point(session)
        if point is None or session.selected_role is None:
            await update.message.reply_text("Сначала выберите точку и роль.", reply_markup=main_menu_keyboard())
            session_store.reset_flow(session)
            return

        status_message = await update.message.reply_text("⏳ Проверяем геопозицию...")

        lat = update.message.location.latitude
        lon = update.message.location.longitude
        accuracy = getattr(update.message.location, "horizontal_accuracy", None)
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None

        point_lat = as_float(point.get("geo_lat"))
        point_lon = as_float(point.get("geo_lon"))
        radius = as_float(point.get("geo_radius_m")) or float(config.DEFAULT_RADIUS_M)

        if point_lat is None or point_lon is None:
            await status_message.edit_text("Не удалось определить координаты точки. Выберите другую точку.")
            return

        if accuracy is None or accuracy > config.ACCURACY_MAX_M:
            session.last_status = STATUS_UNKNOWN
            session.last_distance_m = None
            acc_text = f"{accuracy:.0f}" if accuracy is not None else "неизвестна"
            await status_message.edit_text(
                "⚠️ Не удаётся точно определить местоположение "
                f"(точность {acc_text} м).\n"
                "Включите GPS, выйдите на улицу, подождите 10–20 сек и отправьте трансляцию снова.",
                reply_markup=retry_inline_keyboard(),
            )
            return

        dist_m = haversine_m(lat, lon, point_lat, point_lon)
        session.last_distance_m = dist_m

        if dist_m > radius:
            session.last_status = STATUS_OUT
            await status_message.edit_text(
                "❌ Мы не видим вас в рабочем радиусе точки.\n"
                f"Сейчас: ≈{dist_m:.0f} м, допустимо {radius:.0f} м.\n"
                "Включите GPS, подойдите ближе и отправьте трансляцию снова.",
                reply_markup=retry_inline_keyboard(),
            )
            return

        payload = {
            "point_id": point.get("id"),
            "role": session.selected_role,
            "geo_lat": lat,
            "geo_lon": lon,
            "telegram_user_id": user.id,
            "telegram_chat_id": chat.id,
        }

        try:
            result = await oc_client.shift_start(payload)
        except RuntimeError:
            await status_message.edit_text("Не удалось начать смену: временная ошибка API. Попробуйте ещё раз.")
            return

        if result.get("ok") is False and result.get("error"):
            await status_message.edit_text(f"Не удалось начать смену: {result['error']}")
            return

        shift_id = result.get("shift_id") or result.get("id")
        try:
            session.active_shift_id = int(shift_id) if shift_id is not None else None
        except (TypeError, ValueError):
            session.active_shift_id = None

        session.active = True
        session.active_point_id = point.get("id")
        session.active_point_name = point.get("short_name")
        session.active_point_lat = point_lat
        session.active_point_lon = point_lon
        session.active_point_radius = radius
        session.active_role = session.selected_role
        session.active_started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.consecutive_out_count = 0
        session.last_out_warn_at = 0.0
        session.last_admin_alert_at = 0.0
        session.mode = MODE_IDLE

        await status_message.edit_text(
            "✅ Вы в рабочей зоне "
            f"(≈{dist_m:.0f} м, допустимо {radius:.0f} м).\n"
            "Смена начата. Удачной работы!"
        )
        await update.message.reply_text("Главное меню снова доступно ниже.", reply_markup=main_menu_keyboard())

    return [MessageHandler(filters.LOCATION, handle_location_message)]
