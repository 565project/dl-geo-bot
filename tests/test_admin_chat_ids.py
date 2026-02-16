import unittest
from unittest.mock import patch

from shiftbot import config
from shiftbot.app import ShiftBotApp


class DummyLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


class DummyOpenCartClient:
    def __init__(self, mapping):
        self.mapping = mapping
        self.phones = []

    async def staff_by_phone(self, phone):
        self.phones.append(phone)
        return self.mapping.get(phone)


class AdminChatIdsTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_admin_phone(self):
        self.assertEqual(ShiftBotApp._normalize_admin_phone("8 (903) 326-24-08"), "+79033262408")
        self.assertEqual(ShiftBotApp._normalize_admin_phone("9033262408"), "+79033262408")
        self.assertEqual(ShiftBotApp._normalize_admin_phone("+7 903 326 24 08"), "+79033262408")
        self.assertIsNone(ShiftBotApp._normalize_admin_phone("123"))

    async def test_load_admin_chat_ids_from_config_and_phones(self):
        app = object.__new__(ShiftBotApp)
        app.logger = DummyLogger()
        app.oc_client = DummyOpenCartClient(
            {
                "+79033262408": {"staff_id": 1, "telegram_chat_id": "555"},
                "+79000000000": None,
                "+79001112233": {"staff_id": 2, "telegram_chat_id": None},
            }
        )

        with (
            patch.object(config, "ADMIN_CHAT_IDS", [111, 222]),
            patch.object(config, "ADMIN_CHAT_ID", 333),
            patch.object(
                config,
                "ADMIN_PHONES",
                ["8 (903) 326-24-08", "+7-900-000-00-00", "+7 900 111 22 33", "123"],
            ),
        ):
            result = await app._load_admin_chat_ids()

        self.assertEqual(result, [111, 222, 333, 555])
        self.assertEqual(
            app.oc_client.phones,
            ["+79033262408", "+79000000000", "+79001112233"],
        )
        self.assertIn("admin phone not linked to telegram yet", app.logger.warnings)


if __name__ == "__main__":
    unittest.main()
