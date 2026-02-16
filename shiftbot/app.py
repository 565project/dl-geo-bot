from telegram import BotCommand, Update
from telegram.ext import Application

from shiftbot import config
from shiftbot.dead_soul_detector import DeadSoulDetector
from shiftbot.guards import StaffService
from shiftbot.handlers_location import build_location_handlers
from shiftbot.handlers_shift import build_shift_handlers
from shiftbot.jobs import build_job_check_stale
from shiftbot.opencart_client import OpenCartClient
from shiftbot.registration import build_cancel_handler, build_registration_handler
from shiftbot.session_store import SessionStore
from shiftbot.staff_cache import StaffCache


class ShiftBotApp:
    def __init__(self, logger) -> None:
        self.logger = logger
        self.session_store = SessionStore()
        self.oc_client = OpenCartClient(config.OC_API_BASE, config.OC_API_KEY, logger)
        self.staff_cache = StaffCache(ttl_sec=config.STAFF_CACHE_TTL_SEC)
        self.staff_service = StaffService(self.oc_client, self.staff_cache)
        self.dead_soul_detector = DeadSoulDetector(
            bucket_sec=config.DEAD_SOUL_BUCKET_SEC,
            window_sec=config.DEAD_SOUL_WINDOW_SEC,
            streak_threshold=config.DEAD_SOUL_STREAK,
            alert_cooldown_sec=config.ALERT_COOLDOWN_DEAD_SEC,
        )
        self.admin_chat_ids: list[int] = []

        if not config.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN пуст.")
        if not config.OC_API_BASE or not config.OC_API_KEY:
            raise RuntimeError("OC_API_BASE и OC_API_KEY обязательны.")

        self.application = (
            Application.builder()
            .token(config.BOT_TOKEN)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )

    async def _post_init(self, app: Application) -> None:
        commands = [
            BotCommand("start", "Запустить бота и открыть меню"),
            BotCommand("status", "Показать статус смены"),
            BotCommand("restart", "Сбросить сценарий"),
            BotCommand("start_shift", "Начать смену"),
            BotCommand("stop_shift", "Завершить смену"),
            BotCommand("help", "Краткая инструкция"),
        ]
        await app.bot.set_my_commands(commands)

        admin_chat_ids: list[int] = []
        for phone in config.ADMIN_PHONES:
            try:
                staff = await self.oc_client.staff_by_phone(phone)
            except Exception as exc:
                self.logger.warning("ADMIN_PHONE_RESOLVE_FAILED phone=%s error=%s", phone, exc)
                continue

            if not isinstance(staff, dict):
                self.logger.warning("ADMIN_PHONE_NOT_FOUND phone=%s", phone)
                continue

            chat_id_raw = staff.get("telegram_chat_id")
            try:
                chat_id = int(chat_id_raw) if chat_id_raw is not None else 0
            except (TypeError, ValueError):
                chat_id = 0

            if chat_id <= 0:
                self.logger.warning("ADMIN_CHAT_ID_MISSING phone=%s staff_id=%s", phone, staff.get("staff_id"))
                continue
            admin_chat_ids.append(chat_id)

        self.admin_chat_ids = sorted(set(admin_chat_ids))
        app.bot_data["admin_chat_ids"] = self.admin_chat_ids
        self.logger.info("ADMIN_CHAT_IDS_LOADED count=%s", len(self.admin_chat_ids))

    async def _post_shutdown(self, app: Application) -> None:
        await self.oc_client.aclose()

    def register_handlers(self, app: Application) -> None:
        app.add_handler(build_registration_handler(self.staff_service, self.oc_client, self.logger))
        app.add_handler(build_cancel_handler())

        for handler in build_shift_handlers(
            self.session_store,
            self.staff_service,
            self.oc_client,
            self.dead_soul_detector,
            self.logger,
        ):
            app.add_handler(handler)

        for handler in build_location_handlers(
            self.session_store,
            self.staff_service,
            self.oc_client,
            self.dead_soul_detector,
            self.logger,
        ):
            app.add_handler(handler)

        if config.ENABLE_STALE_CHECK:
            if app.job_queue is None:
                raise RuntimeError(
                    "JobQueue отсутствует. Установи:\n"
                    "python -m pip install \"python-telegram-bot[job-queue]\""
                )
            app.job_queue.run_repeating(
                build_job_check_stale(self.session_store, self.logger),
                interval=config.STALE_CHECK_EVERY_SEC,
                first=config.STALE_CHECK_EVERY_SEC,
            )

    def run(self) -> None:
        self.register_handlers(self.application)
        print("Bot started (polling). Ctrl+C to stop.")
        self.application.run_polling(
            allowed_updates=["message", "edited_message", "callback_query"],
        )
