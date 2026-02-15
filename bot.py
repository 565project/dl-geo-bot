import math
import os
import time
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OC_API_BASE = os.getenv("OC_API_BASE")
OC_API_KEY = os.getenv("OC_API_KEY")

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

STAFF_CACHE_TTL_SEC = 30
HTTP_TIMEOUT_SEC = 10

REG_NAME, REG_CONTACT, REG_TYPE = range(3)

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
# OpenCart API
# =========================

def _require_oc_config() -> Tuple[str, str]:
    if not OC_API_BASE or not OC_API_KEY:
        raise RuntimeError("OC_API_BASE/OC_API_KEY не заданы.")
    return OC_API_BASE, OC_API_KEY


async def oc_get_staff(telegram_user_id: int) -> Optional[dict]:
    api_base, api_key = _require_oc_config()
    url = f"{api_base}?route=dl/geo_api/staff_by_telegram&key={api_key}&telegram_user_id={telegram_user_id}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("API_ERROR action=staff_by_telegram user=%s error=%s", telegram_user_id, exc)
        raise RuntimeError("temporary_api_error") from exc

    staff = payload.get("staff") if isinstance(payload, dict) else None
    return staff if isinstance(staff, dict) else None


async def oc_register(payload: dict) -> dict:
    api_base, api_key = _require_oc_config()
    url = f"{api_base}?route=dl/geo_api/register&key={api_key}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("API_ERROR action=register user=%s error=%s", payload.get("telegram_user_id"), exc)
        raise RuntimeError("temporary_api_error") from exc

    return data if isinstance(data, dict) else {"error": "Некорректный ответ API"}


async def get_staff_cached(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_user_id: int,
    *,
    force_refresh: bool = False,
) -> Optional[dict]:
    cache: Dict[int, Tuple[float, Optional[dict]]] = context.application.bot_data.setdefault("staff_cache", {})
    now = time.time()

    if not force_refresh:
        cached = cache.get(telegram_user_id)
        if cached:
            ts, staff = cached
            if (now - ts) <= STAFF_CACHE_TTL_SEC:
                return staff

    staff = await oc_get_staff(telegram_user_id)
    cache[telegram_user_id] = (now, staff)
    return staff


# =========================
# REGISTRATION
# =========================

def normalize_ru_phone(raw: str) -> Optional[str]:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("8"):
        digits = "7" + digits[1:]

    if len(digits) != 11 or not digits.startswith("7"):
        return None

    return f"+{digits}"


def active_menu_text() -> str:
    return "Команды: /start_shift /stop_shift /status"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    try:
        staff = await get_staff_cached(context, user.id, force_refresh=True)
    except RuntimeError:
        await update.message.reply_text("Временная ошибка связи.")
        return ConversationHandler.END

    if staff is None:
        logger.info("REG_START user=%s", user.id)
        context.user_data["reg"] = {}
        await update.message.reply_text("Привет! Для начала регистрации отправь ФИО текстом.")
        return REG_NAME

    if int(staff.get("is_active", 0)) == 0:
        logger.info("BLOCKED_ACCESS user=%s reason=inactive_on_start", user.id)
        await update.message.reply_text("Аккаунт заблокирован/заморожен, обратитесь к администратору.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Вы уже зарегистрированы.\n" + active_menu_text(),
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return REG_NAME

    full_name = (update.message.text or "").strip()
    if not full_name:
        await update.message.reply_text("Введите ФИО текстом.")
        return REG_NAME

    reg = context.user_data.setdefault("reg", {})
    reg["full_name"] = full_name

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text("Отправьте ваш контакт кнопкой ниже.", reply_markup=keyboard)
    return REG_CONTACT


async def reg_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not update.message:
        return REG_CONTACT

    contact = update.message.contact
    if not contact:
        await update.message.reply_text("Пожалуйста, отправьте контакт кнопкой.")
        return REG_CONTACT

    if contact.user_id != user.id:
        await update.message.reply_text("Нужно отправить именно ваш контакт.")
        return REG_CONTACT

    phone = normalize_ru_phone(contact.phone_number or "")
    if phone is None:
        await update.message.reply_text("Не удалось распознать номер. Отправьте корректный российский номер.")
        return REG_CONTACT

    reg = context.user_data.setdefault("reg", {})
    reg["phone"] = phone

    inline = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Штат", callback_data="emp:staff")],
            [InlineKeyboardButton("Подработка", callback_data="emp:part_time")],
        ]
    )
    await update.message.reply_text(
        "Выберите тип занятости:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("Тип занятости:", reply_markup=inline)
    return REG_TYPE


async def reg_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat

    if not query or not user or not chat:
        return ConversationHandler.END

    await query.answer()
    employment_type = query.data.split(":", maxsplit=1)[1]

    reg = context.user_data.get("reg", {})
    payload = {
        "telegram_user_id": user.id,
        "telegram_chat_id": chat.id,
        "full_name": reg.get("full_name"),
        "phone": reg.get("phone"),
        "employment_type": employment_type,
    }

    try:
        result = await oc_register(payload)
    except RuntimeError:
        logger.info("REG_FAIL user=%s reason=api_error", user.id)
        await query.message.reply_text("Временная ошибка связи.")
        return ConversationHandler.END

    if result.get("error"):
        logger.info("REG_FAIL user=%s reason=%s", user.id, result.get("error"))
        await query.message.reply_text(f"Регистрация не завершена: {result.get('error')}")
        return ConversationHandler.END

    is_active = int(result.get("is_active", 0))
    cache: Dict[int, Tuple[float, Optional[dict]]] = context.application.bot_data.setdefault("staff_cache", {})
    cache[user.id] = (time.time(), {"is_active": is_active, "staff_id": result.get("staff_id")})

    if is_active == 1:
        logger.info("REG_DONE user=%s staff_id=%s", user.id, result.get("staff_id"))
        await query.message.reply_text("Регистрация завершена.\n" + active_menu_text())
    else:
        logger.info("REG_DONE user=%s staff_id=%s inactive=1", user.id, result.get("staff_id"))
        await query.message.reply_text("Аккаунт заблокирован/заморожен, обратитесь к администратору.")

    context.user_data.pop("reg", None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    if update.message:
        await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def ensure_staff_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False

    try:
        staff = await get_staff_cached(context, user.id)
    except RuntimeError:
        if update.effective_message:
            await update.effective_message.reply_text("Временная ошибка связи.")
        return False

    if staff is None:
        logger.info("BLOCKED_ACCESS user=%s reason=not_registered", user.id)
        if update.effective_message:
            await update.effective_message.reply_text("Сначала зарегистрируйся через /start")
        return False

    if int(staff.get("is_active", 0)) == 0:
        logger.info("BLOCKED_ACCESS user=%s reason=inactive", user.id)
        if update.effective_message:
            await update.effective_message.reply_text("Аккаунт заблокирован/заморожен, обратитесь к администратору.")
        return False

    return True


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

async def cmd_start_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not await ensure_staff_active(update, context):
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

    if not await ensure_staff_active(update, context):
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

    if not await ensure_staff_active(update, context):
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

    if not await ensure_staff_active(update, context):
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
    if not OC_API_BASE or not OC_API_KEY:
        raise RuntimeError("OC_API_BASE и OC_API_KEY обязательны.")

    app = Application.builder().token(BOT_TOKEN).build()

    reg_conversation = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_CONTACT: [MessageHandler(filters.CONTACT, reg_contact)],
            REG_TYPE: [CallbackQueryHandler(reg_type, pattern=r"^emp:(staff|part_time)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(reg_conversation)
    app.add_handler(CommandHandler("cancel", cmd_cancel))
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
