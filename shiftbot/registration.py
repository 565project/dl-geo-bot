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

from shiftbot.guards import inactive_staff_text
from shiftbot.handlers_shift import show_main_menu

REG_CONTACT, REG_CONFIRM, REG_NAME, REG_TYPE = range(4)


def normalize_ru_phone(raw: str) -> Optional[str]:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("8"):
        digits = "7" + digits[1:]

    if len(digits) != 11 or not digits.startswith("7"):
        return None

    return f"+{digits}"


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


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

        if staff is not None:
            if int(staff.get("is_active", 0)) == 0:
                logger.info("BLOCKED_ACCESS user=%s reason=inactive_on_start", user.id)
                await update.message.reply_text(inactive_staff_text(staff))
                return ConversationHandler.END

            await show_main_menu(update, context, "Вы уже зарегистрированы ✅")
            return ConversationHandler.END

        logger.info("REG_START user=%s", user.id)
        context.user_data["reg"] = {}
        await update.message.reply_text(
            "Привет! Вы начинаете работу в Доброланч 👋\n"
            "Чтобы начать, пройдём регистрацию — всего 3 простых шага:\n"
            "1) Отправить контакт\n"
            "2) Написать ФИО полностью\n"
            "3) Выбрать тип занятости"
        )
        await update.message.reply_text(
            "Нажмите кнопку ниже, чтобы отправить контакт.\n"
            "Когда Telegram спросит разрешение — не переживайте: номер видит только администрация компании 🙂",
            reply_markup=contact_keyboard(),
        )
        return REG_CONTACT

    async def reg_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if not user or not update.message:
            return REG_CONTACT

        contact = update.message.contact
        if not contact:
            await update.message.reply_text("Пожалуйста, отправьте контакт кнопкой ниже.", reply_markup=contact_keyboard())
            return REG_CONTACT

        if contact.user_id != user.id:
            await update.message.reply_text("Пожалуйста, отправьте именно свой контакт.", reply_markup=contact_keyboard())
            return REG_CONTACT

        phone = normalize_ru_phone(contact.phone_number or "")
        if phone is None:
            await update.message.reply_text(
                "Не удалось распознать номер. Отправьте корректный российский номер.",
                reply_markup=contact_keyboard(),
            )
            return REG_CONTACT

        try:
            found_staff = await staff_service.get_staff_by_phone(phone)
        except RuntimeError:
            await update.message.reply_text("Временная ошибка связи.")
            return ConversationHandler.END

        reg = context.user_data.setdefault("reg", {})
        reg["phone"] = phone

        if found_staff is None:
            await update.message.reply_text(
                "Отлично! Теперь напишите ваше ФИО полностью:",
                reply_markup=ReplyKeyboardRemove(),
            )
            return REG_NAME

        if int(found_staff.get("is_active", 0)) == 0:
            await update.message.reply_text(inactive_staff_text(found_staff), reply_markup=ReplyKeyboardRemove())
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        if str(found_staff.get("telegram_user_id") or "") == str(user.id):
            await show_main_menu(update, context, "Вы уже зарегистрированы ✅")
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        reg["found_staff"] = {
            "staff_id": found_staff.get("staff_id"),
            "full_name": found_staff.get("full_name") or "(без имени)",
            "phone": found_staff.get("phone") or phone,
            "prev_telegram_chat_id": found_staff.get("telegram_chat_id"),
            "prev_telegram_user_id": found_staff.get("telegram_user_id"),
        }

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Да, это я", callback_data="staff_confirm:yes")],
                [InlineKeyboardButton("Новое устройство", callback_data="staff_confirm:new_device")],
                [InlineKeyboardButton("Нет", callback_data="staff_confirm:no")],
            ]
        )
        await update.message.reply_text(
            "В базе уже зарегистрирован сотрудник:\n"
            f"{reg['found_staff']['full_name']} — {reg['found_staff']['phone']}\n"
            "Это вы?",
            reply_markup=ReplyKeyboardRemove(),
        )
        await update.message.reply_text("Выберите вариант:", reply_markup=keyboard)
        return REG_CONFIRM

    async def reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if not query or not user or not chat:
            return ConversationHandler.END

        await query.answer()
        decision = query.data.split(":", maxsplit=1)[1]
        reg = context.user_data.get("reg", {})
        found_staff = reg.get("found_staff") if isinstance(reg, dict) else None
        if not isinstance(found_staff, dict) or not found_staff.get("staff_id"):
            await query.message.reply_text("Сессия регистрации устарела. Начните заново: /start")
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)

        if decision == "no":
            await query.message.reply_text(
                "Этот номер уже привязан к другому аккаунту.\n"
                "Для работы каждый номер закреплён за одним Telegram-аккаунтом.\n"
                "Напишите с аккаунта с этим номером или обратитесь к администратору."
            )
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        mode = "confirm" if decision == "yes" else "new_device"
        try:
            result = await staff_service.rebind_telegram(
                staff_id=int(found_staff["staff_id"]),
                telegram_user_id=user.id,
                telegram_chat_id=chat.id,
                mode=mode,
            )
        except RuntimeError:
            await query.message.reply_text("Временная ошибка связи.")
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        if not result.get("ok"):
            error_text = result.get("error")
            if error_text:
                await query.message.reply_text(f"Ошибка: {error_text}")
            else:
                await query.message.reply_text("Временная ошибка связи.")
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        staff_service.cache.invalidate(user.id)

        if mode == "new_device":
            prev_chat_id = result.get("prev_telegram_chat_id") or found_staff.get("prev_telegram_chat_id")
            try:
                prev_chat_id_int = int(prev_chat_id) if prev_chat_id is not None else None
            except (TypeError, ValueError):
                prev_chat_id_int = None

            if prev_chat_id_int and prev_chat_id_int != chat.id:
                try:
                    await context.bot.send_message(
                        chat_id=prev_chat_id_int,
                        text=(
                            "⚠️ Сессия в этом чате завершена: вход выполнен с нового устройства.\n"
                            "Если это ошибка — сообщите руководителю."
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("NEW_DEVICE_NOTIFY_FAIL chat_id=%s error=%s", prev_chat_id_int, exc)

            await query.message.reply_text("Готово! Новое устройство привязано ✅")
            await show_main_menu(update, context, "Теперь можно работать через кнопки меню.")
        else:
            await query.message.reply_text("Готово! Вы авторизованы ✅")
            await show_main_menu(update, context, "Теперь можно работать через кнопки меню.")

        context.user_data.pop("reg", None)
        return ConversationHandler.END

    async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not update.message:
            return REG_NAME

        full_name = (update.message.text or "").strip()
        if not full_name:
            await update.message.reply_text("Отлично! Теперь напишите ваше ФИО полностью:")
            return REG_NAME

        reg = context.user_data.setdefault("reg", {})
        reg["full_name"] = full_name

        inline = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Штат", callback_data="emp:staff")],
                [InlineKeyboardButton("Подработка", callback_data="emp:part_time")],
            ]
        )
        await update.message.reply_text("Вы штатный сотрудник или подрабатываете?", reply_markup=inline)
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
            result = await oc_client.register(payload)
        except RuntimeError:
            logger.info("REG_FAIL user=%s reason=api_error", user.id)
            await query.message.reply_text("Временная ошибка связи.")
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        if result.get("error"):
            logger.info("REG_FAIL user=%s reason=%s", user.id, result.get("error"))
            await query.message.reply_text(f"Регистрация не завершена: {result.get('error')}")
            context.user_data.pop("reg", None)
            return ConversationHandler.END

        is_active = int(result.get("is_active", 0))
        staff_service.cache.invalidate(user.id)

        if is_active == 1:
            logger.info("REG_DONE user=%s staff_id=%s", user.id, result.get("staff_id"))
            await show_main_menu(update, context, "Спасибо за регистрацию! ✅ Выберите действие:")
        else:
            logger.info("REG_DONE user=%s staff_id=%s inactive=1", user.id, result.get("staff_id"))
            await query.message.reply_text(inactive_staff_text(result))

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
            REG_CONTACT: [
                MessageHandler(filters.CONTACT, reg_contact),
                MessageHandler(~filters.COMMAND, reg_contact),
            ],
            REG_CONFIRM: [CallbackQueryHandler(reg_confirm, pattern=r"^staff_confirm:(yes|no|new_device)$")],
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_TYPE: [CallbackQueryHandler(reg_type, pattern=r"^emp:(staff|part_time)$")],
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
