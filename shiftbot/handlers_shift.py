import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from shiftbot import config
from shiftbot.guards import ensure_staff_active
from shiftbot.models import STATUS_IDLE, STATUS_UNKNOWN


def build_shift_handlers(session_store, staff_service, logger):
    async def cmd_start_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat or not update.message:
            return

        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)
        session.active = True
        session.out_streak = 0
        session.last_warn_ts = 0.0
        session.last_stale_notify_ts = 0.0
        session.last_status = STATUS_UNKNOWN
        session.last_notified_status = STATUS_IDLE
        session.last_ping_ts = 0.0
        session.last_valid_ping_ts = 0.0
        session.last_distance_m = None
        session.last_accuracy_m = None

        logger.info("SHIFT_START user=%s chat=%s", user.id, chat.id)

        await update.message.reply_text(
            "✅ Смена начата.\n\n"
            "Теперь отправь Live Location:\n"
            "📎 → Геопозиция → *Транслировать геопозицию* → *8 часов*.\n\n"
            f"Геозона: радиус *{config.RADIUS_M} м*.\n"
            f"Макс. точность: *{config.ACCURACY_MAX_M} м*.\n"
            f"Точка: `{config.POINT_LAT}, {config.POINT_LON}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_stop_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat or not update.message:
            return

        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)
        if not session.active:
            await update.message.reply_text("Смена не активна. Чтобы начать: /start_shift")
            return

        session.active = False
        session.last_status = STATUS_IDLE
        session.last_notified_status = STATUS_IDLE
        logger.info("SHIFT_STOP user=%s chat=%s", user.id, chat.id)

        await update.message.reply_text("🛑 Смена завершена. Live Location можешь остановить вручную в Telegram.")

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat or not update.message:
            return

        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)
        if not session.active:
            await update.message.reply_text("Статус: смена не активна. /start_shift")
            return

        now = time.time()
        age = (now - session.last_ping_ts) if session.last_ping_ts else None
        dist = f"{session.last_distance_m:.0f} м" if session.last_distance_m is not None else "—"
        acc = f"{session.last_accuracy_m:.0f} м" if session.last_accuracy_m is not None else "—"
        age_txt = f"{age:.0f} сек" if age is not None else "—"

        await update.message.reply_text(
            f"Статус: *{session.last_status}*\n"
            f"Дистанция: *{dist}* (радиус {config.RADIUS_M} м)\n"
            f"Точность: *{acc}* (лимит {config.ACCURACY_MAX_M} м)\n"
            f"Последний пинг: *{age_txt} назад*\n"
            f"OUT streak: *{session.out_streak}*",
            parse_mode=ParseMode.MARKDOWN,
        )

    return [
        CommandHandler("start_shift", cmd_start_shift),
        CommandHandler("stop_shift", cmd_stop_shift),
        CommandHandler("status", cmd_status),
    ]
