from telegram import Update
from telegram.ext import ContextTypes


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


async def ensure_staff_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    staff_service: StaffService,
    logger,
) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False

    try:
        staff = await staff_service.get_staff(user.id)
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
