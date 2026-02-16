import math
import time
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from shiftbot import config
from shiftbot.geo import haversine_m
from shiftbot.live_registry import LIVE_REGISTRY
from shiftbot import guards
from shiftbot.guards import ensure_staff_active
from shiftbot.handlers_shift import active_shift_keyboard, main_menu_keyboard
from shiftbot.models import MODE_AWAITING_LOCATION, MODE_IDLE, STATUS_IN, STATUS_OUT
from shiftbot.opencart_client import ApiUnavailableError


def build_location_handlers(session_store, staff_service, oc_client, logger):
    role_map = {
        "cashier": "cashier",
        "baker": "baker",
        "both": "both",
        "кассир": "cashier",
        "повар": "baker",
        "кассир+повар": "both",
        "кассир/повар": "both",
        "оба": "both",
    }

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
            [InlineKeyboardButton("📍 Отправить ещё раз", callback_data="send_location")],
            [InlineKeyboardButton("🔁 Сменить точку", callback_data="change_point")],
        ]
        if include_issue:
            rows.append([InlineKeyboardButton("🆘 Сообщить об ошибке", callback_data="report_issue")])
        return InlineKeyboardMarkup(rows)

    def api_retry_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Повторить", callback_data="send_location")]])

    def out_alert_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🆘 Сообщить об ошибке", callback_data="report_issue")],
                [InlineKeyboardButton("🔁 Сменить точку", callback_data="change_point")],
            ]
        )

    async def maybe_notify_admin(context, text: str) -> None:
        if config.ADMIN_CHAT_ID <= 0:
            logger.warning("ADMIN_CHAT_ID_NOT_SET")
            return
        await context.bot.send_message(chat_id=config.ADMIN_CHAT_ID, text=text)

    async def handle_active_shift_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE, session, location) -> None:
        if session.active_point_lat is None or session.active_point_lon is None or not session.active_shift_id:
            return

        now = time.time()
        lat = location.latitude
        lon = location.longitude
        accuracy = getattr(location, "horizontal_accuracy", None)
        dist_m = haversine_m(lat, lon, session.active_point_lat, session.active_point_lon)
        radius_m = session.active_point_radius or float(config.DEFAULT_RADIUS_M)
        in_zone = dist_m <= radius_m

        session.last_ping_ts = now
        session.last_distance_m = dist_m
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None
        session.last_valid_ping_ts = now
        session.last_lat = lat
        session.last_lon = lon
        session.last_acc = float(accuracy) if accuracy is not None else None
        session.last_dist_m = dist_m

        if in_zone:
            session.last_status = STATUS_IN
            session.out_streak = 0
            session.consecutive_out_count = 0
        else:
            session.last_status = STATUS_OUT
            session.out_streak += 1
            session.consecutive_out_count = min(session.consecutive_out_count + 1, config.OUT_LIMIT)

            if session.out_streak == config.OUT_STREAK_ALERT and (now - session.last_warn_ts) >= config.NOTIFY_COOLDOWN_SEC:
                session.last_warn_ts = now
                await update.message.reply_text("⚠️ Вы вне рабочей зоны, вернитесь на точку и проверьте Live Location.")

                staff = await staff_service.get_staff(session.user_id)
                staff_id = (staff or {}).get("staff_id") or "—"
                await maybe_notify_admin(
                    context,
                    (
                        "🚨 OUT streak alert\n"
                        f"shift#{session.active_shift_id} staff#{staff_id}\n"
                        f"point#{session.active_point_id or '—'} dist≈{dist_m:.0f}м r={radius_m:.0f}м\n"
                        f"OUT подряд: {session.out_streak}"
                    ),
                )

        bucket = math.floor(now / config.GPS_BUCKET_SEC)
        bucket_key = f"{round(lat, 5)}:{round(lon, 5)}:{bucket}"
        if session.last_bucket_key == bucket_key:
            session.same_bucket_hits += 1
        else:
            session.same_bucket_hits = 1
            session.last_bucket_key = bucket_key

        staff = await staff_service.get_staff(session.user_id)
        staff_id = None
        tg_user_id = None
        if staff:
            try:
                staff_id = int(staff.get("staff_id")) if staff.get("staff_id") is not None else None
            except (TypeError, ValueError):
                staff_id = None
            try:
                tg_user_id = int(staff.get("telegram_user_id")) if staff.get("telegram_user_id") is not None else None
            except (TypeError, ValueError):
                tg_user_id = None

        LIVE_REGISTRY.cleanup_stale(stale_timeout_sec=600, now_ts=now)
        LIVE_REGISTRY.upsert_shift(
            shift_id=session.active_shift_id,
            staff_id=staff_id,
            tg_user_id=tg_user_id,
            point_id=session.active_point_id,
            bucket_key=bucket_key,
            now_ts=now,
        )

        same_shifts = LIVE_REGISTRY.get_same_signature_shifts(session.active_shift_id, bucket_key)
        touched_pairs: set[str] = set()

        for other_shift_id, other_data in same_shifts:
            pair_key, streak = LIVE_REGISTRY.touch_pair(session.active_shift_id, other_shift_id, bucket_key, now_ts=now)
            touched_pairs.add(pair_key)
            if streak < config.SAME_GPS_STREAK_ALERT:
                continue
            if not LIVE_REGISTRY.can_notify_pair(pair_key, config.NOTIFY_COOLDOWN_SEC, now_ts=now):
                continue

            me = LIVE_REGISTRY.get_shift(session.active_shift_id) or {}
            await maybe_notify_admin(
                context,
                (
                    "⚠️ Возможные мёртвые души: "
                    f"shift#{session.active_shift_id} staff#{me.get('staff_id', '—')} "
                    f"и shift#{other_shift_id} staff#{other_data.get('staff_id', '—')} "
                    f"совпадают по GPS {streak} раз подряд, точка {session.active_point_id or '—'}"
                ),
            )

        LIVE_REGISTRY.clear_shift_pairs_except(session.active_shift_id, touched_pairs)

        if config.DEBUG_GPS_NOTIFY and (now - session.last_notify_ts) >= config.GPS_BUCKET_SEC:
            session.last_notify_ts = now
            zone_label = "IN" if in_zone else "OUT"
            await maybe_notify_admin(
                context,
                (
                    f"[DEBUG] shift#{session.active_shift_id} zone={zone_label} "
                    f"dist≈{dist_m:.0f}м r={radius_m:.0f}м out_streak={session.out_streak} "
                    f"same_bucket_hits={session.same_bucket_hits}"
                ),
            )

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

        if session.selected_role is None or session.selected_point_id is None:
            await update.message.reply_text("Сначала выберите точку и роль.", reply_markup=main_menu_keyboard())
            session_store.reset_flow(session)
            return

        status_message = await update.message.reply_text("⏳ Проверяем геопозицию...")

        lat = update.message.location.latitude
        lon = update.message.location.longitude
        accuracy = getattr(update.message.location, "horizontal_accuracy", None)
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None

        point_lat_raw = as_float(session.selected_point_lat)
        point_lon_raw = as_float(session.selected_point_lon)
        base_radius = as_float(session.selected_point_radius) or float(config.DEFAULT_RADIUS_M)
        user_id = user.id
        mode = session.mode
        acc_text = f"{accuracy:.0f}" if accuracy is not None else "неизвестна"

        staff = await guards.get_staff_or_reply(update, context, staff_service, logger)
        if not staff:
            return

        try:
            oc_staff_id = int(staff["staff_id"])
            tg_user_id = int(staff["telegram_user_id"])
        except (KeyError, TypeError, ValueError):
            logger.error("GEO_GATE_STAFF_IDS_INVALID staff=%s", staff)
            await status_message.edit_text(
                "Не удалось начать смену: сотрудник не найден/не активен. Напишите администратору."
            )
            return

        if point_lat_raw is None or point_lon_raw is None:
            state_snapshot = dict(vars(session))
            logger.info("[GEO_GATE] missing point coords, state=%s", state_snapshot)
            _geolog(f"[GEO_GATE] result=UNKNOWN reason=point_coords_missing state={state_snapshot}")
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
            f"[GEO_GATE] user={user_id} staff_id={oc_staff_id} tg_user_id={tg_user_id} "
            f"mode={mode} attempt={attempt_num}/{config.GATE_MAX_ATTEMPTS} "
            f"user=({lat:.7f},{lon:.7f}) "
            f"point=({point_lat:.7f},{point_lon:.7f}) "
            f"dist={dist_m:.1f}m base_r={base_radius} eff_r={effective_radius} "
            f"acc={accuracy} acc_max={config.ACCURACY_MAX_M}"
        )

        logger.info(
            "[GEO_GATE] staff_id=%s point_id=%s attempt=%s/%s user=(%.6f,%.6f) "
            "point_raw=(%.6f,%.6f) point_used=(%.6f,%.6f) dist_m=%.1f acc=%s acc_max=%s base_r=%.1f eff_radius=%.1f",
            oc_staff_id,
            session.selected_point_id,
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
                    "Мы не видим вас в рабочем радиусе: "
                    f"≈{dist_m:.0f} м, допустимо сейчас {effective_radius:.0f} м (попытка {session.gate_attempt}/{config.GATE_MAX_ATTEMPTS}).\n"
                    "Подойдите ближе к точке и отправьте локацию ещё раз.\n\n"
                    f"{details}",
                    reply_markup=retry_inline_keyboard(),
                )
                return

            await status_message.edit_text(
                f"Мы не видим вас в рабочем радиусе после нескольких попыток.\n"
                "Проверьте точку и отправьте геопозицию ещё раз.\n\n"
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

        role_raw = str(session.selected_role).strip().lower()
        role = role_map.get(role_raw)
        if role not in {"cashier", "baker", "both"}:
            logger.warning("SHIFT_START_ROLE_INVALID role=%r mapped=%r", session.selected_role, role)
            await status_message.edit_text("Не удалось начать смену: некорректная роль. Выберите роль заново.")
            return

        payload = {
            "staff_id": oc_staff_id,
            "point_id": session.selected_point_id,
            "role": role,
            "start_lat": lat,
            "start_lon": lon,
            "start_acc": float(accuracy) if accuracy is not None else None,
        }

        logger.info(
            "CALL shift_start staff_id=%s point_id=%s role=%s lat=%r lon=%r acc=%r",
            payload["staff_id"],
            payload["point_id"],
            payload["role"],
            payload["start_lat"],
            payload["start_lon"],
            payload["start_acc"],
        )

        try:
            result = await oc_client.shift_start(payload)
        except ApiUnavailableError:
            await status_message.edit_text(
                "Сайт временно недоступен (ошибка сети). Попробуйте ещё раз через 10 секунд.",
                reply_markup=api_retry_keyboard(),
            )
            return

        if not isinstance(result, dict):
            await status_message.edit_text("Не удалось начать смену: некорректный ответ API.")
            return

        success = result.get("success")
        if success is False:
            error_json = result.get("json") if isinstance(result.get("json"), dict) else None
            error_code = (error_json or {}).get("error") or "bad_request"
            if result.get("status") == 409 and error_code == "shift_already_active":
                shift_id = (error_json or {}).get("shift_id")
                try:
                    session.active_shift_id = int(shift_id) if shift_id is not None else None
                except (TypeError, ValueError):
                    session.active_shift_id = None
                session.active_started_at = (error_json or {}).get("started_at") or session.active_started_at
                await status_message.edit_text(
                    f"У вас уже активная смена #{session.active_shift_id or '—'} (с {session.active_started_at or '—'}).",
                    reply_markup=active_shift_keyboard(),
                )
                return
            if error_code == "staff_not_found":
                await status_message.edit_text(
                    "Не удалось начать смену: сотрудник не найден/не активен. Напишите администратору."
                )
                return
            await status_message.edit_text(f"Не удалось начать смену: {error_code}")
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
        session.active_point_id = session.selected_point_id
        session.active_point_name = session.selected_point_name
        session.active_point_lat = point_lat
        session.active_point_lon = point_lon
        session.active_point_radius = base_radius
        session.active_role = role
        session.active_started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.consecutive_out_count = 0
        session.out_streak = 0
        session.last_bucket_key = None
        session.same_bucket_hits = 0
        session.last_ping_ts = 0.0
        session.last_notify_ts = 0.0
        session.last_lat = None
        session.last_lon = None
        session.last_acc = None
        session.last_dist_m = None
        session.same_gps_signature = None
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
