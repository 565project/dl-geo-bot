import time

from telegram.ext import ContextTypes

from shiftbot import config
from shiftbot.models import STATUS_UNKNOWN
from shiftbot.violation_alerts import maybe_send_admin_notify_from_decision

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

                session.last_stale_notify_ts = now
                session.last_status = STATUS_UNKNOWN
                session.out_streak = 0
                logger.info("STALE user=%s age=%.1f -> UNKNOWN", session.user_id, age)

                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=(
                        "❓ Давно нет обновлений Live Location.\n"
                        "Проверь, что трансляция геопозиции активна и Telegram имеет доступ к геолокации.\n\n"
                        "Если смена закончилась — /stop_shift."
                    ),
                )

                await _refresh_active_shift_if_needed(session, now)

                if not session.active_shift_id:
                    logger.warning("STALE_TICK_SKIP_NO_ACTIVE_SHIFT user=%s", session.user_id)
                    _stop_monitoring_session(session)
                    continue

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

                if isinstance(response, dict) and response.get("error") == "shift_not_active":
                    logger.warning(
                        "VIOLATION_TICK_SHIFT_NOT_ACTIVE user=%s shift_id=%s -> stop monitoring",
                        session.user_id,
                        session.active_shift_id,
                    )
                    _stop_monitoring_session(session)
                    await _refresh_active_shift_if_needed(session, now)
                    if not session.active_shift_id:
                        continue

                await maybe_send_admin_notify_from_decision(
                    context=context,
                    response=response,
                    shift_id=session.active_shift_id,
                    logger=logger,
                    staff_name=getattr(session, "active_staff_name", None),
                    point_id=session.active_point_id,
                    last_ping_ts=session.last_ping_ts,
                )

    return job_check_stale
