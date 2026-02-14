import math
import time
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# НАСТРОЙКИ (ТЕСТ)
# =========================

BOT_TOKEN = "8105246434:AAH-6lBOMulCmgGoKlsFNVNftV6mYRh8K1Q"

POINT_LAT = 56.628495
POINT_LON = 47.894357

RADIUS_M = 120              # радиус геозоны (м)
ACCURACY_MAX_M = 50         # если точность хуже -> UNKNOWN (без штрафов)

OUT_STREAK_REQUIRED = 2     # антифлап: предупреждение после N подряд OUT
WARN_COOLDOWN_SEC = 120     # антиспам: не чаще 1 предупреждения в N секунд

# Проверка "давно нет обновлений"
ENABLE_STALE_CHECK = True
STALE_CHECK_EVERY_SEC = 30
STALE_AFTER_SEC = 90
STALE_NOTIFY_COOLDOWN_SEC = 180

# =========================
# ЛОГИ
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("shiftbot")

# =========================
# ПАМЯТЬ (СЕССИИ)
# =========================

@dataclass
class ShiftSession:
    user_id: int
    chat_id: int
    active: bool = False

    last_ping_ts: float = 0.0
    last_valid_ping_ts: float = 0.0

    out_streak: int = 0
    last_warn_ts: float = 0.0

    last_stale_notify_ts: float = 0.0

    last_distance_m: Optional[float] = None
    last_accuracy_m: Optional[float] = None
    last_status: str = "IDLE"             # IDLE / IN / OUT / UNKNOWN
    last_notified_status: str = "IDLE"    # чтобы слать сообщения только при смене статуса


SESSIONS: Dict[int, ShiftSession] = {}


def get_or_create_session(user_id: int, chat_id: int) -> ShiftSession:
    s = SESSIONS.get(user_id)
    if not s:
        s = ShiftSession(user_id=user_id, chat_id=chat_id)
        SESSIONS[user_id] = s
    else:
        s.chat_id = chat_id
    return s


# =========================
# GEO
# =========================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


# =========================
# КОМАНДЫ
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Это тестовый бот учёта смен через Live Location.\n\n"
        "Команды:\n"
        "• /start_shift — начать смену\n"
        "• /stop_shift — завершить смену\n"
        "• /status — показать последний статус\n\n"
        "После /start_shift отправь: 📎 → Геопозиция → Транслировать геопозицию (Live) → 8 часов."
    )


async def cmd_start_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    s = get_or_create_session(user.id, chat.id)
    s.active = True
    s.out_streak = 0
    s.last_warn_ts = 0.0
    s.last_stale_notify_ts = 0.0
    s.last_status = "UNKNOWN"
    s.last_notified_status = "IDLE"
    s.last_ping_ts = 0.0
    s.last_valid_ping_ts = 0.0
    s.last_distance_m = None
    s.last_accuracy_m = None

    logger.info("SHIFT_START user=%s chat=%s", user.id, chat.id)

    await update.message.reply_text(
        "✅ Смена начата.\n\n"
        "Теперь отправь Live Location:\n"
        "📎 → Геопозиция → *Транслировать геопозицию* → *8 часов*.\n\n"
        f"Геозона: радиус *{RADIUS_M} м*.\n"
        f"Макс. точность: *{ACCURACY_MAX_M} м*.\n"
        f"Точка: `{POINT_LAT}, {POINT_LON}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stop_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    s = get_or_create_session(user.id, chat.id)
    if not s.active:
        await update.message.reply_text("Смена не активна. Чтобы начать: /start_shift")
        return

    s.active = False
    s.last_status = "IDLE"
    s.last_notified_status = "IDLE"
    logger.info("SHIFT_STOP user=%s chat=%s", user.id, chat.id)

    await update.message.reply_text("🛑 Смена завершена. Live Location можешь остановить вручную в Telegram.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    s = get_or_create_session(user.id, chat.id)
    if not s.active:
        await update.message.reply_text("Статус: смена не активна. /start_shift")
        return

    now = time.time()
    age = (now - s.last_ping_ts) if s.last_ping_ts else None

    dist = f"{s.last_distance_m:.0f} м" if s.last_distance_m is not None else "—"
    acc = f"{s.last_accuracy_m:.0f} м" if s.last_accuracy_m is not None else "—"
    age_txt = f"{age:.0f} сек" if age is not None else "—"

    await update.message.reply_text(
        f"Статус: *{s.last_status}*\n"
        f"Дистанция: *{dist}* (радиус {RADIUS_M} м)\n"
        f"Точность: *{acc}* (лимит {ACCURACY_MAX_M} м)\n"
        f"Последний пинг: *{age_txt} назад*\n"
        f"OUT streak: *{s.out_streak}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# =========================
# LOCATION HANDLERS
# =========================

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

    s = get_or_create_session(user.id, chat.id)
    if not s.active:
        await msg.reply_text("Смена не активна. Нажми /start_shift")
        return

    loc = msg.location
    lat, lon = loc.latitude, loc.longitude
    accuracy = getattr(loc, "horizontal_accuracy", None)
    now = time.time()

    s.last_ping_ts = now
    s.last_accuracy_m = float(accuracy) if accuracy is not None else None

    src = "edited_message" if is_edited else "message"

    logger.info(
        "PING src=%s user=%s lat=%.6f lon=%.6f acc=%s",
        src, user.id, lat, lon, f"{accuracy:.1f}" if accuracy is not None else "None"
    )

    # 1) Gate by accuracy
    if accuracy is None or accuracy > ACCURACY_MAX_M:
        s.last_status = "UNKNOWN"
        s.out_streak = 0
        s.last_distance_m = None

        logger.info("STATUS=UNKNOWN reason=accuracy acc=%s", accuracy)

        if s.last_notified_status != "UNKNOWN":
            s.last_notified_status = "UNKNOWN"
            await context.bot.send_message(
                chat_id=s.chat_id,
                text=f"ℹ️ UNKNOWN: точность плохая ({accuracy} м). Жду точную геопозицию.",
            )
        return

    # 2) Distance
    dist_m = haversine_m(lat, lon, POINT_LAT, POINT_LON)
    s.last_distance_m = dist_m

    # 3) IN / OUT
    if dist_m <= RADIUS_M:
        s.last_status = "IN"
        s.last_valid_ping_ts = now
        s.out_streak = 0

        logger.info("STATUS=IN dist=%.1f radius=%s acc=%.1f", dist_m, RADIUS_M, accuracy)

        if s.last_notified_status != "IN":
            s.last_notified_status = "IN"
            await context.bot.send_message(
                chat_id=s.chat_id,
                text=f"✅ IN: в зоне. dist={dist_m:.0f}м, acc={accuracy:.0f}м",
            )
        return

    # OUT
    s.last_status = "OUT"
    s.out_streak += 1

    logger.info(
        "STATUS=OUT dist=%.1f radius=%s acc=%.1f out_streak=%d",
        dist_m, RADIUS_M, accuracy, s.out_streak
    )

    if s.last_notified_status != "OUT":
        s.last_notified_status = "OUT"
        await context.bot.send_message(
            chat_id=s.chat_id,
            text=f"⚠️ OUT: вне зоны (пока без подтверждения). dist={dist_m:.0f}м, acc={accuracy:.0f}м",
        )

    # 4) Confirmed OUT warning (anti-flap + cooldown)
    if s.out_streak >= OUT_STREAK_REQUIRED and (now - s.last_warn_ts) >= WARN_COOLDOWN_SEC:
        s.last_warn_ts = now
        await context.bot.send_message(
            chat_id=s.chat_id,
            text=(
                "🚨 *Подтверждено: вы вне геозоны*.\n"
                f"• Дистанция: *{dist_m:.0f} м* (радиус *{RADIUS_M} м*)\n"
                f"• Точность: *{accuracy:.0f} м*\n\n"
                "Проверьте точную геолокацию / GPS.\n"
                "Если смена закончилась — /stop_shift."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


# =========================
# STALE CHECK (JOBQUEUE)
# =========================

async def job_check_stale(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not SESSIONS:
        return

    now = time.time()
    for s in list(SESSIONS.values()):
        if not s.active:
            continue
        if s.last_ping_ts <= 0:
            continue

        age = now - s.last_ping_ts
        if age >= STALE_AFTER_SEC:
            if (now - s.last_stale_notify_ts) < STALE_NOTIFY_COOLDOWN_SEC:
                continue

            s.last_stale_notify_ts = now
            s.last_status = "UNKNOWN"
            s.out_streak = 0

            logger.info("STALE user=%s age=%.1f -> UNKNOWN", s.user_id, age)

            await context.bot.send_message(
                chat_id=s.chat_id,
                text=(
                    "❓ Давно нет обновлений Live Location.\n"
                    "Проверь, что трансляция геопозиции активна и Telegram имеет доступ к геолокации.\n\n"
                    "Если смена закончилась — /stop_shift."
                ),
            )


# =========================
# MAIN
# =========================

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пуст.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("start_shift", cmd_start_shift))
    app.add_handler(CommandHandler("stop_shift", cmd_stop_shift))
    app.add_handler(CommandHandler("status", cmd_status))

    # first live-location message
    app.add_handler(MessageHandler(filters.LOCATION & ~filters.UpdateType.EDITED_MESSAGE, handle_location_message))
    # live-location updates
    app.add_handler(MessageHandler(filters.LOCATION & filters.UpdateType.EDITED_MESSAGE, handle_location_edited))

    if ENABLE_STALE_CHECK:
        if app.job_queue is None:
            raise RuntimeError(
                "JobQueue отсутствует. Установи:\n"
                "python -m pip install \"python-telegram-bot[job-queue]\""
            )
        app.job_queue.run_repeating(job_check_stale, interval=STALE_CHECK_EVERY_SEC, first=STALE_CHECK_EVERY_SEC)

    print("Bot started (polling). Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
