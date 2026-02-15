import time

from telegram.ext import ContextTypes

from shiftbot import config
from shiftbot.models import STATUS_UNKNOWN


def build_job_check_stale(session_store, logger):
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

    return job_check_stale
