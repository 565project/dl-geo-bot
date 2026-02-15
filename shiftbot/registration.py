from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from shiftbot import config


def normalize_ru_phone(raw: str) -> Optional[str]:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("8"):
        digits = "7" + digits[1:]

    if len(digits) != 11 or not digits.startswith("7"):
        return None

    return f"+{digits}"


def active_menu_text() -> str:
    return "Команды: /start_shift /stop_shift /status"


def build_registration_handler(staff_service, oc_client, logger) -> ConversationHandler:
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not user or not update.message:
            return ConversationHandler.END

        try:
            staff = await staff_service.get_staff(user.id, force_refresh=True)
        except RuntimeError:
            await update.message.reply_text("Временная ошибка связи.")
            return ConversationHandler.END

        if staff is None:
            logger.info("REG_START user=%s", user.id)
            context.user_data["reg"] = {}
            await update.message.reply_text("Привет! Для начала регистрации отправь ФИО текстом.")
            return config.REG_NAME

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
            return config.REG_NAME

        full_name = (update.message.text or "").strip()
        if not full_name:
            await update.message.reply_text("Введите ФИО текстом.")
            return config.REG_NAME

        reg = context.user_data.setdefault("reg", {})
        reg["full_name"] = full_name

        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("Отправить контакт", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text("Отправьте ваш контакт кнопкой ниже.", reply_markup=keyboard)
        return config.REG_CONTACT

    async def reg_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not user or not update.message:
            return config.REG_CONTACT

        contact = update.message.contact
        if not contact:
            await update.message.reply_text("Пожалуйста, отправьте контакт кнопкой.")
            return config.REG_CONTACT

        if contact.user_id != user.id:
            await update.message.reply_text("Нужно отправить именно ваш контакт.")
            return config.REG_CONTACT

        phone = normalize_ru_phone(contact.phone_number or "")
        if phone is None:
            await update.message.reply_text("Не удалось распознать номер. Отправьте корректный российский номер.")
            return config.REG_CONTACT

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
        return config.REG_TYPE

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
            result = await oc_client.register(payload)
        except RuntimeError:
            logger.info("REG_FAIL user=%s reason=api_error", user.id)
            await query.message.reply_text("Временная ошибка связи.")
            return ConversationHandler.END

        if result.get("error"):
            logger.info("REG_FAIL user=%s reason=%s", user.id, result.get("error"))
            await query.message.reply_text(f"Регистрация не завершена: {result.get('error')}")
            return ConversationHandler.END

        is_active = int(result.get("is_active", 0))
        staff_service.cache.set(user.id, {"is_active": is_active, "staff_id": result.get("staff_id")})

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

    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            config.REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            config.REG_CONTACT: [MessageHandler(filters.CONTACT, reg_contact)],
            config.REG_TYPE: [CallbackQueryHandler(reg_type, pattern=r"^emp:(staff|part_time)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )


def build_cancel_handler() -> CommandHandler:
    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("reg", None)
        if update.message:
            await update.message.reply_text("Регистрация отменена.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    return CommandHandler("cancel", cmd_cancel)
