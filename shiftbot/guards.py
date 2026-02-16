from telegram import Update
from telegram.ext import ContextTypes


def inactive_staff_text(staff: dict) -> str:
    status = (staff.get("status") or "").strip().lower()
    if status == "blocked":
        return "Аккаунт заблокирован. Обратитесь к администратору."
    if status == "frozen":
        return "Аккаунт заморожен. Обратитесь к администратору."
    return "Аккаунт заблокирован/заморожен, обратитесь к администратору."


class StaffService:
    def __init__(self, client, cache) -> None:
        self.client = client
        self.cache = cache

    async def get_staff(self, telegram_user_id: int, *, force_refresh: bool = False):
        if not force_refresh:
            hit, cached = self.cache.get(telegram_user_id)
            if hit:
                return cached
        staff = await self.client.get_staff(telegram_user_id)
        self.cache.set(telegram_user_id, staff)
        return staff

    async def get_staff_by_phone(self, phone_raw: str):
        return await self.client.staff_by_phone(phone_raw)

    async def rebind_telegram(
        self,
        staff_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        mode: str,
    ):
        return await self.client.rebind_telegram(
            staff_id=staff_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            mode=mode,
        )


async def ensure_staff_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    staff_service: StaffService,
    logger,
) -> bool:
    staff = await get_staff_or_reply(update, context, staff_service, logger)
    return staff is not None


async def get_staff_or_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    staff_service: StaffService,
    logger,
) -> dict | None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return None

    try:
        staff = await staff_service.get_staff(user.id)
    except RuntimeError:
        if update.effective_message:
            await update.effective_message.reply_text("Временная ошибка связи.")
        return None

    if staff is None:
        logger.info("BLOCKED_ACCESS user=%s reason=not_registered", user.id)
        if update.effective_message:
            await update.effective_message.reply_text("Сначала зарегистрируйся через /start")
        return None

    if int(staff.get("is_active", 0)) == 0:
        logger.info("BLOCKED_ACCESS user=%s reason=inactive", user.id)
        if update.effective_message:
            await update.effective_message.reply_text(inactive_staff_text(staff))
        return None

    return staff
