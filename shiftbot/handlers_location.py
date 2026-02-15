import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, MessageHandler, filters

from shiftbot import config
from shiftbot.geo import haversine_m
from shiftbot.guards import ensure_staff_active
from shiftbot.models import STATUS_IN, STATUS_OUT, STATUS_UNKNOWN


def build_location_handlers(session_store, staff_service, logger):
    async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.location:
            return
        await process_location(update, context, is_edited=False)

    async def handle_location_edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.edited_message or not update.edited_message.location:
            return
        await process_location(update, context, is_edited=True)

    async def process_location(update: Update, context: ContextTypes.DEFAULT_TYPE, is_edited: bool) -> None:
        msg = update.edited_message if is_edited else update.message
        if not msg or not msg.location:
            return

        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return

        if not await ensure_staff_active(update, context, staff_service, logger):
            return

        session = session_store.get_or_create(user.id, chat.id)
        if not session.active:
            await msg.reply_text("Смена не активна. Нажми /start_shift")
            return

        loc = msg.location
        lat, lon = loc.latitude, loc.longitude
        accuracy = getattr(loc, "horizontal_accuracy", None)
        now = time.time()

        session.last_ping_ts = now
        session.last_accuracy_m = float(accuracy) if accuracy is not None else None

        src = "edited_message" if is_edited else "message"
        logger.info(
            "PING src=%s user=%s lat=%.6f lon=%.6f acc=%s",
            src,
            user.id,
            lat,
            lon,
            f"{accuracy:.1f}" if accuracy is not None else "None",
        )

        if accuracy is None or accuracy > config.ACCURACY_MAX_M:
            session.last_status = STATUS_UNKNOWN
            session.out_streak = 0
            session.last_distance_m = None
            logger.info("STATUS=UNKNOWN reason=accuracy acc=%s", accuracy)

            if session.last_notified_status != STATUS_UNKNOWN:
                session.last_notified_status = STATUS_UNKNOWN
                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=f"ℹ️ UNKNOWN: точность плохая ({accuracy} м). Жду точную геопозицию.",
                )
            return

        dist_m = haversine_m(lat, lon, config.POINT_LAT, config.POINT_LON)
        session.last_distance_m = dist_m

        if dist_m <= config.RADIUS_M:
            session.last_status = STATUS_IN
            session.last_valid_ping_ts = now
            session.out_streak = 0

            logger.info("STATUS=IN dist=%.1f radius=%s acc=%.1f", dist_m, config.RADIUS_M, accuracy)

            if session.last_notified_status != STATUS_IN:
                session.last_notified_status = STATUS_IN
                await context.bot.send_message(
                    chat_id=session.chat_id,
                    text=f"✅ IN: в зоне. dist={dist_m:.0f}м, acc={accuracy:.0f}м",
                )
            return

        session.last_status = STATUS_OUT
        session.out_streak += 1
        logger.info(
            "STATUS=OUT dist=%.1f radius=%s acc=%.1f out_streak=%d",
            dist_m,
            config.RADIUS_M,
            accuracy,
            session.out_streak,
        )

        if session.last_notified_status != STATUS_OUT:
            session.last_notified_status = STATUS_OUT
            await context.bot.send_message(
                chat_id=session.chat_id,
                text=f"⚠️ OUT: вне зоны (пока без подтверждения). dist={dist_m:.0f}м, acc={accuracy:.0f}м",
            )

        if (
            session.out_streak >= config.OUT_STREAK_REQUIRED
            and (now - session.last_warn_ts) >= config.WARN_COOLDOWN_SEC
        ):
            session.last_warn_ts = now
            await context.bot.send_message(
                chat_id=session.chat_id,
                text=(
                    "🚨 *Подтверждено: вы вне геозоны*.\n"
                    f"• Дистанция: *{dist_m:.0f} м* (радиус *{config.RADIUS_M} м*)\n"
                    f"• Точность: *{accuracy:.0f} м*\n\n"
                    "Проверьте точную геолокацию / GPS.\n"
                    "Если смена закончилась — /stop_shift."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )

    return [
        MessageHandler(filters.LOCATION & ~filters.UpdateType.EDITED_MESSAGE, handle_location_message),
        MessageHandler(filters.LOCATION & filters.UpdateType.EDITED_MESSAGE, handle_location_edited),
    ]
