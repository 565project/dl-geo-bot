from telegram import Update
from telegram.ext import Application

from shiftbot import config
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

        if not config.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN пуст.")
        if not config.OC_API_BASE or not config.OC_API_KEY:
            raise RuntimeError("OC_API_BASE и OC_API_KEY обязательны.")

        self.application = Application.builder().token(config.BOT_TOKEN).build()

    def register_handlers(self, app: Application) -> None:
        app.add_handler(build_registration_handler(self.staff_service, self.oc_client, self.logger))
        app.add_handler(build_cancel_handler())

        for handler in build_shift_handlers(self.session_store, self.staff_service, self.logger):
            app.add_handler(handler)

        for handler in build_location_handlers(self.session_store, self.staff_service, self.logger):
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
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
