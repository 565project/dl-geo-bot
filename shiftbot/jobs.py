import time

from telegram.ext import ContextTypes

from shiftbot import config
from shiftbot.models import STATUS_UNKNOWN
from shiftbot.admin_notify import notify_admins

ACTIVE_SHIFT_REFRESH_EVERY_SEC = 300


def build_job_check_stale(session_store, oc_client, logger):
    def _as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _sync_shift_fields(session, shift: dict | None) -> None:
        if not isinstance(shift, dict):
            session.active_shift_id = None
            session.active = False
            return

        # Shift closed or suspended on the server side — treat as inactive
        if shift.get("ended_at") or str(shift.get("override_status") or "").lower() == "suspended":
            session.active_shift_id = None
            session.active = False
            return

        shift_id = _as_int(shift.get("shift_id") or shift.get("id"))
        session.active_shift_id = shift_id
        session.active = shift_id is not None

        point_id = _as_int(shift.get("point_id"))
        if point_id is not None:
            session.active_point_id = point_id
        session.active_point_name = shift.get("point_name") or session.active_point_name

        point_lat = shift.get("point_lat") or shift.get("geo_lat") or shift.get("lat")
        point_lon = shift.get("point_lon") or shift.get("geo_lon") or shift.get("lon")
        point_radius = shift.get("point_radius") or shift.get("geo_radius_m") or shift.get("radius")

        try:
            session.active_point_lat = float(point_lat) if point_lat is not None else session.active_point_lat
        except (TypeError, ValueError):
            pass
        try:
            session.active_point_lon = float(point_lon) if point_lon is not None else session.active_point_lon
        except (TypeError, ValueError):
            pass
        try:
            session.active_point_radius = float(point_radius) if point_radius is not None else session.active_point_radius
        except (TypeError, ValueError):
            pass

        if shift.get("role"):
            session.active_role = str(shift.get("role"))
        if shift.get("staff_name") or shift.get("full_name"):
            session.active_staff_name = shift.get("staff_name") or shift.get("full_name")
        if shift.get("started_at"):
            session.active_started_at = shift.get("started_at")

    def _stop_monitoring_session(session) -> None:
        if hasattr(session_store, "clear_shift_state"):
            session_store.clear_shift_state(session)
            return
        session.active = False
        session.active_shift_id = None

    async def _refresh_active_shift_if_needed(session, now: float) -> None:
        refresh_age = now - getattr(session, "last_active_shift_refresh_ts", 0.0)
        if session.active_shift_id and refresh_age < ACTIVE_SHIFT_REFRESH_EVERY_SEC:
            return

        try:
            staff = await oc_client.get_staff_by_telegram(session.user_id)
        except Exception as exc:
            logger.warning("STALE_SHIFT_REFRESH_STAFF_FAILED user=%s error=%s", session.user_id, exc)
            return

        if not isinstance(staff, dict):
            logger.info("STALE_SHIFT_REFRESH_NO_STAFF user=%s", session.user_id)
            return

        staff_id = _as_int(staff.get("staff_id"))
        if staff_id is None:
            logger.warning("STALE_SHIFT_REFRESH_BAD_STAFF_ID user=%s staff=%s", session.user_id, staff)
            return

        try:
            shift = await oc_client.get_active_shift_by_staff(staff_id)
        except Exception as exc:
            logger.warning(
                "STALE_SHIFT_REFRESH_ACTIVE_FAILED user=%s staff_id=%s error=%s",
                session.user_id,
                staff_id,
                exc,
            )
            return

        session.last_active_shift_refresh_ts = now
        _sync_shift_fields(session, shift)
        logger.info(
            "STALE_SHIFT_REFRESH user=%s staff_id=%s refreshed_shift_id=%s",
            session.user_id,
            staff_id,
            session.active_shift_id,
        )

    async def job_check_stale(context: ContextTypes.DEFAULT_TYPE) -> None:
        if session_store.is_empty():
            return

        now = time.time()
        for session in list(session_store.values()):
            if not session.active:
                continue
            if session.last_ping_ts <= 0:
                continue

            age = now - session.last_ping_ts
            if age >= config.STALE_AFTER_SEC:
                if (now - session.last_stale_notify_ts) < config.STALE_NOTIFY_COOLDOWN_SEC:
                    continue

                # Force-refresh shift status before doing anything else so we
                # don't spam a stale warning for a shift that's already closed.
                session.last_active_shift_refresh_ts = 0.0
                await _refresh_active_shift_if_needed(session, now)

                if not session.active_shift_id:
                    logger.info(
                        "STALE_SHIFT_ALREADY_ENDED user=%s -> stop monitoring", session.user_id
                    )
                    _stop_monitoring_session(session)
                    try:
                        await context.bot.send_message(
                            chat_id=session.chat_id,
                            text="⚠️ Ваша смена завершена администратором. Если это ошибка — свяжитесь с нами.",
                        )
                    except Exception as exc:
                        logger.error("SHIFT_ENDED_NOTIFY_FAIL chat_id=%s error=%s", session.chat_id, exc)
                    continue

                session.last_stale_notify_ts = now
                session.last_status = STATUS_UNKNOWN
                session.out_streak = 0
                logger.info("STALE user=%s age=%.1f -> UNKNOWN", session.user_id, age)

                warn_round = int(getattr(session, "last_out_violation_notified_round", 0) or 0)
                next_round = warn_round + 1
                session.last_out_violation_notified_round = next_round

                if next_round == 1:
                    session.stale_first_detected_ts = now
                    staff_warning_text = (
                        "⚠️ Мы вас не видим. Пожалуйста, включите трансляцию геопозиции."
                        "\n\nПосле второго уведомления смена закроется автоматически, "
                        "а администратор проведет проверку. "
                        "Если это ошибка, смену восстановят без потери рабочего времени."
                    )
                    await context.bot.send_message(
                        chat_id=session.chat_id,
                        text=staff_warning_text,
                    )
                    continue

                shift_id_to_stop = session.active_shift_id
                staff_name = getattr(session, "active_staff_name", None) or f"{session.user_id}"
                point_label = getattr(session, "active_point_name", None) or (
                    f"id={getattr(session, 'active_point_id', None)}"
                    if getattr(session, "active_point_id", None) is not None
                    else "—"
                )
                staff_phone = getattr(session, "active_staff_phone", None) or "не указан"
                admin_text = (
                    f"Сотрудник {staff_name} пропал с радаров на точке {point_label}.\n"
                    f"Телефон сотрудника: {staff_phone}\n\n"
                    "Требуется ручная проверка по камерам. "
                    "Заявка на подозрение отправлена на сайт для рассмотрения."
                )
                await notify_admins(
                    context,
                    admin_text,
                    shift_id=shift_id_to_stop,
                    cooldown_key="admin_notify_stale",
                )

                end_at_ts = int(getattr(session, "stale_first_detected_ts", 0.0) or now)

                auto_stopped = False
                stop_result = None
                end_reasons = ["auto_stale_no_geo_second_notice", "auto_violation_out", "manual"]
                for end_reason in end_reasons:
                    try:
                        stop_result = await oc_client.shift_end(
                            {
                                "shift_id": shift_id_to_stop,
                                "end_reason": end_reason,
                                "end_at": end_at_ts,
                            }
                        )
                    except Exception as exc:
                        logger.error(
                            "AUTO_STOP_STALE_SHIFT_FAILED shift_id=%s reason=%s error=%s",
                            shift_id_to_stop,
                            end_reason,
                            exc,
                        )
                        continue

                    is_error = bool(stop_result.get("ok") is False and stop_result.get("error"))
                    error_payload = (stop_result.get("json") or {}) if isinstance(stop_result, dict) else {}
                    error_code = str(error_payload.get("error") or stop_result.get("error") or "").strip().lower()

                    if not is_error:
                        auto_stopped = True
                        logger.info(
                            "AUTO_STOP_STALE_SHIFT_ACCEPTED shift_id=%s reason=%s result=%s",
                            shift_id_to_stop,
                            end_reason,
                            stop_result,
                        )
                        break

                    if error_code != "bad_end_reason":
                        logger.warning(
                            "AUTO_STOP_STALE_SHIFT_REJECTED shift_id=%s reason=%s error=%s result=%s",
                            shift_id_to_stop,
                            end_reason,
                            error_code,
                            stop_result,
                        )
                        break

                    logger.warning(
                        "AUTO_STOP_STALE_SHIFT_BAD_REASON shift_id=%s reason=%s -> retry",
                        shift_id_to_stop,
                        end_reason,
                    )

                if auto_stopped:
                    # Verify on server that shift is truly closed.
                    session.last_active_shift_refresh_ts = 0.0
                    await _refresh_active_shift_if_needed(session, now)
                    auto_stopped = not bool(session.active_shift_id)

                logger.info(
                    "AUTO_STOP_STALE_SHIFT shift_id=%s round=%s auto_stopped=%s result=%s end_at=%s",
                    shift_id_to_stop,
                    next_round,
                    auto_stopped,
                    stop_result,
                    end_at_ts,
                )

                if auto_stopped:
                    _stop_monitoring_session(session)
                    try:
                        await context.bot.send_message(
                            chat_id=session.chat_id,
                            text=(
                                "🔴 Смена закрыта автоматически после повторной потери геопозиции.\n"
                                "Администратор проведет проверку. Если это ошибка — смену восстановят "
                                "без потери рабочего времени."
                            ),
                        )
                    except Exception as exc:
                        logger.error("AUTO_STOP_STALE_NOTIFY_FAIL chat_id=%s error=%s", session.chat_id, exc)
                    continue

                logger.warning(
                    "AUTO_STOP_STALE_SHIFT_NOT_CONFIRMED shift_id=%s result=%s",
                    shift_id_to_stop,
                    stop_result,
                )
                logger.info(
                    "VIOLATION_TICK_PRECHECK user=%s shift_id=%s last_ping_ts=%s last_live_update_ts=%s mode=%s active=%s",
                    session.user_id,
                    session.active_shift_id,
                    session.last_ping_ts,
                    getattr(session, "last_live_update_ts", 0.0),
                    getattr(session, "mode", None),
                    getattr(session, "active", None),
                )

                try:
                    response = await oc_client.violation_tick(session.active_shift_id)
                except Exception as exc:
                    logger.error("VIOLATION_TICK_FAILED shift_id=%s error=%s", session.active_shift_id, exc)
                    continue

                decisions = response.get("decisions", {}) if isinstance(response, dict) else {}
                admin_chat_ids = response.get("admin_chat_ids", []) if isinstance(response, dict) else []
                logger.info(
                    "VIOLATION_TICK_RESPONSE shift_id=%s response=%s",
                    session.active_shift_id,
                    str(response)[:500],
                )
                logger.info(
                    "VIOLATION_TICK_DECISIONS shift_id=%s decisions=%s admin_chat_ids=%s",
                    session.active_shift_id,
                    decisions,
                    admin_chat_ids,
                )

                if isinstance(response, dict) and response.get("error") == "shift_not_active":
                    logger.warning(
                        "VIOLATION_TICK_SHIFT_NOT_ACTIVE user=%s shift_id=%s -> stop monitoring",
                        session.user_id,
                        session.active_shift_id,
                    )
                    _stop_monitoring_session(session)
                    try:
                        await context.bot.send_message(
                            chat_id=session.chat_id,
                            text="⚠️ Ваша смена завершена администратором. Если это ошибка — свяжитесь с нами.",
                        )
                    except Exception as exc:
                        logger.error("SHIFT_ENDED_NOTIFY_FAIL chat_id=%s error=%s", session.chat_id, exc)
                    continue

                if decisions.get("staff_warn"):
                    logger.info(
                        "VIOLATION_TICK_STAFF_WARN_ALREADY_SENT user=%s shift_id=%s",
                        session.user_id,
                        session.active_shift_id,
                    )

                if decisions.get("admin_notify"):
                    shift_id = session.active_shift_id
                    staff_name = getattr(session, "active_staff_name", None) or f"{session.user_id}"
                    point_label = getattr(session, "active_point_name", None) or (
                        f"id={getattr(session, 'active_point_id', None)}"
                        if getattr(session, "active_point_id", None) is not None
                        else "—"
                    )
                    staff_phone = getattr(session, "active_staff_phone", None) or "не указан"

                    admin_text = (
                        f"Сотрудник {staff_name} пропал с радаров на точке {point_label}.\n"
                        f"Телефон сотрудника: {staff_phone}\n\n"
                        "Требуется ручная проверка по камерам. "
                        "Заявка на подозрение отправлена на сайт для рассмотрения."
                    )

                    await notify_admins(
                        context,
                        admin_text,
                        shift_id=shift_id,
                        cooldown_key="admin_notify_stale",
                    )

    return job_check_stale
