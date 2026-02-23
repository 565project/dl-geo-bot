import time
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from shiftbot import config
from shiftbot.geo import haversine_m
from shiftbot.handlers_shift import active_shift_keyboard, main_menu_keyboard
from shiftbot.models import MODE_AWAITING_LOCATION, MODE_IDLE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN
from shiftbot.opencart_client import ApiUnavailableError
from shiftbot.ping_alerts import process_ping_alerts
from shiftbot.violation_alerts import maybe_send_admin_notify_from_decision
from shiftbot.admin_notify import notify_admins


def build_location_handlers(session_store, staff_service, oc_client, dead_soul_detector, logger):
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

    def as_int(value):
        try:
            return int(value)
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

    def clear_active_shift(session) -> None:
        session.active_shift_id = None
        session.active_started_at = None
        session.active_point_id = None
        session.active_point_name = None
        session.active_point_lat = None
        session.active_point_lon = None
        session.active_point_radius = None
        session.active_role = None
        session.active_staff_name = None

    def sync_session_from_shift(session, shift: dict) -> None:
        shift_id = shift.get("shift_id") or shift.get("id")
        try:
            session.active_shift_id = int(shift_id) if shift_id is not None else None
        except (TypeError, ValueError):
            session.active_shift_id = None

        session.active_started_at = shift.get("started_at") or session.active_started_at

        point_id = shift.get("point_id")
        try:
            session.active_point_id = int(point_id) if point_id is not None else session.active_point_id
        except (TypeError, ValueError):
            pass

        session.active_point_name = shift.get("point_name") or session.active_point_name
        session.active_point_lat = as_float(shift.get("point_lat") or shift.get("geo_lat") or shift.get("lat")) or session.active_point_lat
        session.active_point_lon = as_float(shift.get("point_lon") or shift.get("geo_lon") or shift.get("lon")) or session.active_point_lon
        session.active_point_radius = as_float(shift.get("point_radius") or shift.get("geo_radius_m") or shift.get("radius")) or session.active_point_radius
        session.active_role = role_map.get(str(shift.get("role") or "").lower(), session.active_role)
        session.active_staff_name = shift.get("staff_name") or shift.get("full_name") or session.active_staff_name

    async def ensure_active_shift(session, staff_id: int) -> dict | None:
        shift = await oc_client.get_active_shift_by_staff(staff_id)
        if not isinstance(shift, dict):
            clear_active_shift(session)
            return None

        sync_session_from_shift(session, shift)
        return shift

    async def handle_active_shift_monitoring(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message,
        session,
        location,
        *,
        staff_id: int,
    ) -> None:
        if not session.active_shift_id:
            return

        now = time.time()
        lat = location.latitude
        lon = location.longitude
        accuracy = getattr(location, "horizontal_accuracy", None)
        point_lat = session.active_point_lat
        point_lon = session.active_point_lon
        if point_lat is not None and point_lon is not None:
            dist_m = haversine_m(lat, lon, point_lat, point_lon)
            radius_m = session.active_point_radius or float(config.DEFAULT_RADIUS_M)
        else:
            dist_m = None
            radius_m = None

        session.last_ping_ts = now
        session.last_live_update_ts = now
        session.last_distance_m = dist_m
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None
        session.last_valid_ping_ts = now
        session.last_lat = lat
        session.last_lon = lon
        session.last_acc = float(accuracy) if accuracy is not None else None
        session.last_dist_m = dist_m

        try:
            logger.info("CALL ping_add shift_id=%s staff_id=%s", session.active_shift_id, staff_id)
            response = await oc_client.ping_add(
                shift_id=session.active_shift_id,
                staff_id=staff_id,
                lat=lat,
                lon=lon,
                acc=float(accuracy) if accuracy is not None else None,
            )
        except ApiUnavailableError:
            logger.warning("PING_ADD_UNAVAILABLE shift_id=%s staff_id=%s", session.active_shift_id, staff_id)
            return

        await process_ping_alerts(
            response=response,
            context=context,
            staff_chat_id=message.chat_id,
            fallback_shift_id=session.active_shift_id,
            logger=logger,
        )

        await maybe_send_admin_notify_from_decision(
            context=context,
            response=response,
            shift_id=session.active_shift_id,
            logger=logger,
            staff_name=session.active_staff_name,
            point_id=session.active_point_id,
            last_ping_ts=session.last_ping_ts,
        )

        status = str(response.get("status") or "").upper() or STATUS_UNKNOWN
        out_streak = as_int(response.get("out_streak")) or 0
        out_rounds = as_int(response.get("out_violation_rounds")) or 0
        reason = response.get("reason")
        logger.info(
            "PING_ADD shift_id=%s staff_id=%s -> status=%s reason=%s out_streak=%s rounds=%s",
            session.active_shift_id,
            staff_id,
            status,
            reason,
            out_streak,
            out_rounds,
        )
        dist_from_api = as_float(response.get("dist_m"))
        radius_from_api = as_float(response.get("radius_m"))

        if dist_from_api is not None:
            session.last_distance_m = dist_from_api
            session.last_dist_m = dist_from_api
        if radius_from_api is not None:
            session.active_point_radius = radius_from_api

        session.last_status = status
        session.out_streak = max(out_streak, 0)
        session.consecutive_out_count = max(out_streak, 0)

        if status == STATUS_IN:
            session.last_out_violation_notified_round = max(session.last_out_violation_notified_round, 0)

        if status == STATUS_OUT and out_streak == 3:
            if (now - session.last_out_warn_at) >= config.ALERT_COOLDOWN_OUT_SEC:
                session.last_out_warn_at = now
                d = dist_from_api if dist_from_api is not None else dist_m
                r = radius_from_api if radius_from_api is not None else radius_m
                if d is not None and r is not None:
                    await message.reply_text(
                        f"⚠️ Вы вне рабочей зоны (≈{d:.0f} м, радиус {r:.0f} м). Вернитесь на точку.",
                        reply_markup=out_alert_keyboard(),
                    )
                else:
                    await message.reply_text("⚠️ Вы вне рабочей зоны, вернитесь на точку.", reply_markup=out_alert_keyboard())

            if out_rounds > session.last_out_violation_notified_round:
                session.last_out_violation_notified_round = out_rounds
                if out_rounds == 1:
                    admin_text = (
                        "⚠️ Подозрительная активность: зафиксировано 3 OUT подряд. "
                        "Ждём повторного нарушения.\n"
                        f"shift#{session.active_shift_id} staff#{staff_id} point#{session.active_point_id or '—'}"
                    )
                else:
                    admin_text = (
                        "🚨 Повторное нарушение геозоны: запросить проверку.\n"
                        f"shift#{session.active_shift_id} staff#{staff_id} point#{session.active_point_id or '—'}\n"
                        f"out_violation_rounds={out_rounds}"
                    )
                await notify_admins(context, admin_text, shift_id=session.active_shift_id)

        if status == STATUS_UNKNOWN and (now - session.last_unknown_warn_ts) >= config.ALERT_COOLDOWN_OUT_SEC:
            session.last_unknown_warn_ts = now
            await message.reply_text("ℹ️ Не удалось определить статус GPS. Проверьте, что геолокация включена.")
            unknown_admin_text = (
                f"❓ Неизвестный статус GPS\n"
                f"shift_id: {session.active_shift_id or '—'}\n"
                f"staff: {session.active_staff_name or session.user_id}\n"
                f"point_id: {session.active_point_id or '—'}"
            )
            await notify_admins(
                context,
                unknown_admin_text,
                shift_id=session.active_shift_id,
                cooldown_key="unknown_warn",
            )

        sig = f"{round(lat, config.GPS_SIG_ROUND)}:{round(lon, config.GPS_SIG_ROUND)}"
        point_id = session.selected_point_id or session.active_point_id
        alerts = dead_soul_detector.register_ping(
            shift_id=session.active_shift_id,
            staff_id=staff_id,
            point_id=point_id,
            sig=sig,
            now_ts=now,
        )
        for alert in alerts:
            await notify_admins(
                context,
                (
                    "🚨 Мёртвые души: 10 совпадений подряд GPS. "
                    f"staffA={alert['staff_a']} staffB={alert['staff_b']} "
                    f"точка={alert['point_id'] or '—'} sig={alert['sig']}"
                ),
            )

    async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message or not message.location:
            return

        lat = message.location.latitude
        lon = message.location.longitude
        acc = getattr(message.location, "horizontal_accuracy", None)

        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return

        logger.info(
            "LOCATION_UPDATE tg=%s is_edited=%s lat=%s lon=%s acc=%s",
            user.id,
            bool(update.edited_message),
            lat,
            lon,
            acc,
        )

        session = session_store.get_or_create(user.id, chat.id)
        session.last_live_update_ts = time.time()

        staff = await oc_client.get_staff_by_telegram(user.id)
        if not staff:
            logger.info("LOCATION_UPDATE staff_not_found tg=%s", user.id)
            return

        session.active_staff_name = staff.get("full_name") or staff.get("name") or session.active_staff_name

        try:
            oc_staff_id = int(staff["staff_id"])
        except (KeyError, TypeError, ValueError):
            logger.error("LOCATION_STAFF_ID_INVALID staff=%s", staff)
            return

        try:
            await ensure_active_shift(session, oc_staff_id)
        except ApiUnavailableError:
            logger.warning("ACTIVE_SHIFT_RECOVERY_FAILED staff_id=%s", oc_staff_id)
            return

        if session.active_shift_id:
            await handle_active_shift_monitoring(
                update,
                context,
                message,
                session,
                message.location,
                staff_id=oc_staff_id,
            )
        else:
            logger.info("LOCATION_UPDATE no active shift staff_id=%s", oc_staff_id)

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
            await message.reply_text("Сначала выберите точку и роль.", reply_markup=main_menu_keyboard())
            session_store.reset_flow(session)
            return

        status_message = await message.reply_text("⏳ Проверяем геопозицию...")

        accuracy = acc
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None

        point_lat_raw = as_float(session.selected_point_lat)
        point_lon_raw = as_float(session.selected_point_lon)
        base_radius = as_float(session.selected_point_radius) or float(config.DEFAULT_RADIUS_M)
        user_id = user.id
        mode = session.mode
        acc_text = f"{accuracy:.0f}" if accuracy is not None else "неизвестна"

        try:
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
        session.active_staff_name = staff.get("full_name") or staff.get("name") or session.active_staff_name
        session.active_started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.consecutive_out_count = 0
        session.out_streak = 0
        session.last_bucket_key = None
        session.same_bucket_hits = 0
        session.last_ping_ts = 0.0
        session.last_live_update_ts = 0.0
        session.last_active_shift_refresh_ts = 0.0
        session.last_notify_ts = 0.0
        session.last_lat = None
        session.last_lon = None
        session.last_acc = None
        session.last_dist_m = None
        session.same_gps_signature = None
        session.last_out_warn_at = 0.0
        session.last_admin_alert_at = 0.0
        session.last_out_violation_notified_round = 0
        session.last_unknown_warn_ts = 0.0
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
        await message.reply_text("Главное меню снова доступно ниже.", reply_markup=main_menu_keyboard())

        # Task 3: Companion notifications
        try:
            current_point_id = session.active_point_id
            current_shift_id = session.active_shift_id
            new_staff_name = session.active_staff_name or f"сотрудник #{oc_staff_id}"

            if current_point_id is not None:
                active_shifts = await oc_client.get_active_shifts_by_point(current_point_id)
                colleagues = [
                    s for s in active_shifts
                    if (
                        s.get("shift_id") != current_shift_id
                        and s.get("id") != current_shift_id
                        and s.get("staff_id") != oc_staff_id
                    )
                ]

                if not colleagues:
                    await message.reply_text("✅ Смена начата! Удачи в работе!")
                else:
                    names = ", ".join(
                        s.get("full_name") or s.get("staff_name") or f"сотрудник #{s.get('staff_id', '?')}"
                        for s in colleagues
                    )
                    await message.reply_text(f"👥 На точке уже работают: {names}")
                    for colleague in colleagues:
                        colleague_chat_id = colleague.get("telegram_chat_id")
                        if not colleague_chat_id:
                            continue
                        try:
                            await context.bot.send_message(
                                chat_id=int(colleague_chat_id),
                                text=f"👋 К вам на точку присоединился: {new_staff_name}",
                            )
                        except Exception as exc:
                            logger.warning(
                                "COMPANION_NOTIFY_COLLEAGUE_FAILED chat_id=%s error=%s",
                                colleague_chat_id,
                                exc,
                            )
        except Exception as exc:
            logger.warning("COMPANION_NOTIFY_FAILED error=%s", exc)

    return [
        MessageHandler(filters.UpdateType.MESSAGE & filters.LOCATION, handle_location_message),
        MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_location_message),
    ]
