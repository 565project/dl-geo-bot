import time
import logging
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

    def retry_inline_keyboard(include_issue: bool = False) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("📍 Отправить геопозицию", callback_data="send_location")],
            [InlineKeyboardButton("🔁 Сменить точку", callback_data="change_point")],
        ]
        if include_issue:
            rows.append([InlineKeyboardButton("🆘 Сообщить об ошибке", callback_data="report_issue")])
        return InlineKeyboardMarkup(rows)

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

        log = logging.getLogger("geo_gate")
        log.setLevel(logging.INFO)

        def _geolog(msg: str):
            try:
                log.info(msg)
            except Exception:
                pass
            print(msg, flush=True)

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

        point_lat_raw = as_float(point.get("geo_lat"))
        point_lon_raw = as_float(point.get("geo_lon"))
        base_radius = as_float(point.get("geo_radius_m")) or float(config.DEFAULT_RADIUS_M)
        user_id = user.id
        staff_id = session.user_id
        mode = session.mode
        acc_text = f"{accuracy:.0f}" if accuracy is not None else "неизвестна"

        if point_lat_raw is None or point_lon_raw is None:
            _geolog("[GEO_GATE] result=UNKNOWN reason=point_coords_missing")
            await status_message.edit_text(
                "Не удалось определить координаты точки. Выберите другую точку.\n"
                f"Диагностика: dist≈—м, r={base_radius:.0f}м, acc={acc_text}"
            )
            return

        point_lat = point_lat_raw
        point_lon = point_lon_raw
        if abs(point_lat) > 90 and abs(point_lon) <= 90:
            point_lat, point_lon = point_lon, point_lat
            logger.warning("[GEO_GATE] point coords look swapped, auto-fix swap lat/lon")

        logger.info(
            "[GEO_GATE] point_raw=(%.6f,%.6f) point_used=(%.6f,%.6f)",
            point_lat_raw,
            point_lon_raw,
            point_lat,
            point_lon,
        )

        attempt = max(session.gate_attempt, 0)
        effective_radius = base_radius + (attempt * config.GATE_RADIUS_STEP_M)
        dist_m = haversine_m(lat, lon, point_lat, point_lon)
        session.last_distance_m = dist_m
        attempt_num = attempt + 1

        _geolog(
            f"[GEO_GATE] user={user_id} staff_id={staff_id} "
            f"mode={mode} attempt={attempt_num}/{config.GATE_MAX_ATTEMPTS} "
            f"user=({lat:.7f},{lon:.7f}) "
            f"point=({point_lat:.7f},{point_lon:.7f}) "
            f"dist={dist_m:.1f}m base_r={base_radius} eff_r={effective_radius} "
            f"acc={accuracy} acc_max={config.ACCURACY_MAX_M}"
        )

        logger.info(
            "[GEO_GATE] staff_id=%s point_id=%s attempt=%s/%s user=(%.6f,%.6f) "
            "point_raw=(%.6f,%.6f) point_used=(%.6f,%.6f) dist_m=%.1f acc=%s acc_max=%s base_r=%.1f eff_radius=%.1f",
            session.user_id,
            point.get("id"),
            attempt + 1,
            config.GATE_MAX_ATTEMPTS,
            lat,
            lon,
            point_lat_raw,
            point_lon_raw,
            point_lat,
            point_lon,
            dist_m,
            accuracy,
            config.ACCURACY_MAX_M,
            base_radius,
            effective_radius,
        )

        acc_text = f"{accuracy:.0f}" if accuracy is not None else "неизвестна"
        acc_missing_note = "\nℹ️ точность не передана Telegram, проверяем по расстоянию."
        if accuracy is None:
            logger.info("[GEO_GATE] acc=None, continue with distance check")

        if dist_m > effective_radius:
            session.last_status = STATUS_OUT
            session.gate_last_reason = "distance"
            session.gate_attempt = min(session.gate_attempt + 1, config.GATE_MAX_ATTEMPTS)
            out_reason = "distance"
            if accuracy is not None and accuracy > config.ACCURACY_MAX_M:
                out_reason = "distance_with_poor_accuracy"
            if accuracy is None:
                out_reason = "distance_acc_none"
            logger.info(
                "[GEO_GATE] result=OUT reason=%s user=(%.6f,%.6f) point_raw=(%.6f,%.6f) point_used=(%.6f,%.6f) "
                "dist_m=%.1f acc=%s eff_radius=%.1f",
                out_reason,
                lat,
                lon,
                point_lat_raw,
                point_lon_raw,
                point_lat,
                point_lon,
                dist_m,
                accuracy,
                effective_radius,
            )
            _geolog(f"[GEO_GATE] result=OUT reason={out_reason}")

            details = f"Диагностика: dist≈{dist_m:.0f}м, r={effective_radius:.0f}м, acc={acc_text}"
            if accuracy is None:
                details += acc_missing_note

            if session.gate_attempt < config.GATE_MAX_ATTEMPTS:
                await status_message.edit_text(
                    "❌ Вы вне рабочей зоны: "
                    f"≈{dist_m:.0f} м, допустимо сейчас {effective_radius:.0f} м (попытка {session.gate_attempt}/{config.GATE_MAX_ATTEMPTS}).\n"
                    "Подойдите ближе к точке и отправьте локацию ещё раз.\n\n"
                    f"{details}",
                    reply_markup=retry_inline_keyboard(),
                )
                return

            await status_message.edit_text(
                f"❌ Вы {config.GATE_MAX_ATTEMPTS} раз вне зоны. Проверьте, что выбрана правильная точка и отправляете трансляцию.\n"
                "Нажмите 'Сменить точку' или 'Сообщить об ошибке'.\n\n"
                f"{details}",
                reply_markup=retry_inline_keyboard(include_issue=True),
            )
            return

        session.gate_attempt = 0
        session.gate_last_reason = None
        in_reason = "distance"
        if accuracy is not None and accuracy > config.ACCURACY_MAX_M:
            in_reason = "distance_with_poor_accuracy"
        if accuracy is None:
            in_reason = "distance_acc_none"
        logger.info(
            "[GEO_GATE] result=IN reason=%s user=(%.6f,%.6f) point_raw=(%.6f,%.6f) point_used=(%.6f,%.6f) "
            "dist_m=%.1f acc=%s eff_radius=%.1f",
            in_reason,
            lat,
            lon,
            point_lat_raw,
            point_lon_raw,
            point_lat,
            point_lon,
            dist_m,
            accuracy,
            effective_radius,
        )
        _geolog(f"[GEO_GATE] result=IN reason={in_reason}")

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
        session.active_point_radius = base_radius
        session.active_role = session.selected_role
        session.active_started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.consecutive_out_count = 0
        session.last_out_warn_at = 0.0
        session.last_admin_alert_at = 0.0
        session.mode = MODE_IDLE

        success_message = (
            "✅ Вы в рабочей зоне "
            f"(≈{dist_m:.0f} м, допустимо {effective_radius:.0f} м).\n"
        )
        if accuracy is None:
            success_message += "точность не передана Telegram, проверяем по расстоянию.\n"
        elif accuracy > config.ACCURACY_MAX_M:
            success_message += f"GPS неточный: {acc_text}м.\n"
        success_message += "Смена начата. Удачной работы!"

        await status_message.edit_text(success_message)
        await update.message.reply_text("Главное меню снова доступно ниже.", reply_markup=main_menu_keyboard())

    return [MessageHandler(filters.LOCATION, handle_location_message)]
