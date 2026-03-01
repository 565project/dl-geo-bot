import unittest

from shiftbot.ping_alerts import process_ping_alerts


class DummyBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append({"chat_id": chat_id, "text": text})


class DummyApp:
    def __init__(self):
        self.bot_data = {"admin_chat_ids": [1001, 1002]}


class DummyContext:
    def __init__(self):
        self.bot = DummyBot()
        self.application = DummyApp()


class DummyLogger:
    def __init__(self):
        self.errors = []
        self.infos = []

    def error(self, msg, *args):
        self.errors.append(msg % args if args else msg)

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)


class PingAlertsTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_same_location_2_uses_cluster_staff_and_admins_only(self):
        context = DummyContext()
        logger = DummyLogger()

        response = {
            "ok": True,
            "shift_id": 25,
            "admin_alert": "admin_same_location_2",
            "dead_souls_cluster": {
                "point_id": 29,
                "point_name": "Пекарня на Ленина",
                "staff": [
                    {"staff_id": 1, "full_name": "Иванов Иван Иванович", "role": "baker"},
                    {"staff_id": 2, "full_name": "Петров Петр Петрович", "role": "cashier"},
                ],
            },
        }

        await process_ping_alerts(
            response=response,
            context=context,
            staff_chat_id=777,
            fallback_shift_id=25,
            logger=logger,
        )

        self.assertEqual([m["chat_id"] for m in context.bot.messages], [1001, 1002])
        text = context.bot.messages[0]["text"]
        self.assertIn("Точка: 29 (Пекарня на Ленина)", text)
        self.assertIn("• Иванов Иван Иванович — baker", text)
        self.assertIn("• Петров Петр Петрович — cashier", text)
        self.assertIn("5 раз подряд отправлены одинаковые координаты", text)
        self.assertNotIn("chat_id=777", str(context.bot.messages))

    async def test_admin_same_location_2_fallback_to_single_staff(self):
        context = DummyContext()
        logger = DummyLogger()

        response = {
            "alerts": [
                {
                    "type": "admin_same_location_2",
                    "shift_id": 15,
                    "staff_id": 3,
                    "full_name": "Сидоров Сидор Сидорович",
                    "point_id": 42,
                }
            ]
        }

        await process_ping_alerts(
            response=response,
            context=context,
            staff_chat_id=888,
            fallback_shift_id=15,
            logger=logger,
        )

        self.assertEqual([m["chat_id"] for m in context.bot.messages], [1001, 1002])
        text = context.bot.messages[0]["text"]
        self.assertIn("Точка: 42", text)
        self.assertIn("• Сидоров Сидор Сидорович", text)
        self.assertIn("5 раз подряд отправлены одинаковые координаты", text)

    async def test_admin_same_location_2_deduplicates_by_point_not_shift(self):
        context = DummyContext()
        logger = DummyLogger()

        response_staff_1 = {
            "alerts": [
                {
                    "type": "admin_same_location_2",
                    "shift_id": 122,
                    "point_id": 30,
                    "staff_id": 1,
                    "full_name": "Сотрудник 1",
                }
            ]
        }
        response_staff_2 = {
            "alerts": [
                {
                    "type": "admin_same_location_2",
                    "shift_id": 123,
                    "point_id": 30,
                    "staff_id": 2,
                    "full_name": "Сотрудник 2",
                }
            ]
        }

        await process_ping_alerts(
            response=response_staff_1,
            context=context,
            staff_chat_id=777,
            fallback_shift_id=122,
            logger=logger,
        )
        await process_ping_alerts(
            response=response_staff_2,
            context=context,
            staff_chat_id=888,
            fallback_shift_id=123,
            logger=logger,
        )

        # одно уведомление на админа по точке, а не по каждой смене
        self.assertEqual([m["chat_id"] for m in context.bot.messages], [1001, 1002])
        self.assertIn(30, context.application.bot_data.get("dead_soul_recent_alert_by_point", {}))


if __name__ == "__main__":
    unittest.main()
