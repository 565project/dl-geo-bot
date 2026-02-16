import time

from telegram.ext import ContextTypes

from shiftbot import config
from shiftbot.models import STATUS_UNKNOWN
from shiftbot.violation_alerts import maybe_send_admin_notify_from_decision


def build_job_check_stale(session_store, oc_client, logger):
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

                if not session.active_shift_id:
                    logger.warning("STALE_TICK_SKIP_NO_SHIFT user=%s", session.user_id)
                    continue

                try:
                    response = await oc_client.violation_tick(session.active_shift_id)
                except Exception as exc:
                    logger.error("VIOLATION_TICK_FAILED shift_id=%s error=%s", session.active_shift_id, exc)
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
