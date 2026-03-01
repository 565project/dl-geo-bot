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
                is_second_or_more = warn_round >= 1
                session.last_out_violation_notified_round = warn_round + 1

                staff_warning_text = "⚠️ Мы вас не видим. Пожалуйста, включите трансляцию геопозиции."
                if is_second_or_more:
                    staff_warning_text += (
                        "\n\nПосле второго уведомления смена закроется автоматически, "
                        "а администратор проведет проверку. "
                        "Если это ошибка, смену восстановят без потери рабочего времени."
                    )

                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=staff_warning_text,
                )
                stale_admin_text = (
                    f"⏰ Нет обновлений геолокации\n"
                    f"shift_id: {session.active_shift_id or '—'}\n"
                    f"staff: {getattr(session, 'active_staff_name', None) or session.user_id}\n"
                    f"point_id: {session.active_point_id or '—'}\n"
                    f"last_seen: {round(age, 0):.0f}s назад"
                )
                logger.info("STALE_WARN_SUPPRESSED text=%s", stale_admin_text.replace("\n", " | "))

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
                    point_id = getattr(session, "point_id", session.active_point_id)
                    staff_phone = getattr(session, "active_staff_phone", None) or "не указан"

                    admin_text = (
                        f"⚠️ ПОДОЗРЕНИЕ (2-й раунд)\n\n"
                        f"Сотрудник {staff_name} пропал с радаров на точке {point_id}.\n"
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
