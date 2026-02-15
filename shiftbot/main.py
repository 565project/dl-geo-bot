from shiftbot.app import ShiftBotApp
from shiftbot.logging_setup import setup_logging


def main() -> None:
    logger = setup_logging()
    ShiftBotApp(logger).run()
