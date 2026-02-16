import time
import unittest
from types import SimpleNamespace

from shiftbot import config
from shiftbot.jobs import build_job_check_stale
from shiftbot.models import ShiftSession
from shiftbot.violation_alerts import ADMIN_NOTIFY_COOLDOWN_KEY


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class DummyBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class DummySessionStore:
    def __init__(self, sessions):
        self._sessions = sessions

    def is_empty(self):
        return len(self._sessions) == 0

    def values(self):
        return self._sessions


class DummyOcClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    async def violation_tick(self, shift_id: int):
        self.calls.append(shift_id)
        if self.exc:
            raise self.exc
        return self.response


class StaleJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_warns_staff_and_sends_admin_once_per_cooldown(self):
        now = time.time()
        session = ShiftSession(user_id=1, chat_id=100, active=True)
        session.active_shift_id = 555
        session.active_point_id = 12
        session.active_staff_name = "Иван Иванов"
        session.last_ping_ts = now - (config.STALE_AFTER_SEC + 5)

        oc_client = DummyOcClient(
            response={
                "ok": True,
                "decisions": {"admin_notify": True},
                "reason": "VISIBILITY_LOST",
                "round": 2,
                "admin_chat_ids": [9001],
            }
        )

        context = SimpleNamespace(
            bot=DummyBot(),
            application=SimpleNamespace(bot_data={ADMIN_NOTIFY_COOLDOWN_KEY: {}}),
        )

        stale_job = build_job_check_stale(DummySessionStore([session]), oc_client, DummyLogger())

        original_notify_cd = config.STALE_NOTIFY_COOLDOWN_SEC
        original_admin_cd = config.ADMIN_NOTIFY_COOLDOWN_SEC
        config.STALE_NOTIFY_COOLDOWN_SEC = 0
        config.ADMIN_NOTIFY_COOLDOWN_SEC = 3600
        try:
            await stale_job(context)
            await stale_job(context)
        finally:
            config.STALE_NOTIFY_COOLDOWN_SEC = original_notify_cd
            config.ADMIN_NOTIFY_COOLDOWN_SEC = original_admin_cd

        self.assertEqual(oc_client.calls, [555, 555])
        staff_msgs = [m for m in context.bot.messages if m[0] == 100]
        admin_msgs = [m for m in context.bot.messages if m[0] == 9001]
        self.assertEqual(len(staff_msgs), 2)
        self.assertEqual(len(admin_msgs), 1)

    async def test_network_failure_on_tick_keeps_staff_warning_and_no_crash(self):
        now = time.time()
        session = ShiftSession(user_id=2, chat_id=101, active=True)
        session.active_shift_id = 777
        session.last_ping_ts = now - (config.STALE_AFTER_SEC + 5)

        oc_client = DummyOcClient(exc=RuntimeError("endpoint down"))
        context = SimpleNamespace(
            bot=DummyBot(),
            application=SimpleNamespace(bot_data={ADMIN_NOTIFY_COOLDOWN_KEY: {}}),
        )
        stale_job = build_job_check_stale(DummySessionStore([session]), oc_client, DummyLogger())

        original_notify_cd = config.STALE_NOTIFY_COOLDOWN_SEC
        config.STALE_NOTIFY_COOLDOWN_SEC = 0
        try:
            await stale_job(context)
        finally:
            config.STALE_NOTIFY_COOLDOWN_SEC = original_notify_cd

        self.assertEqual(oc_client.calls, [777])
        self.assertEqual(len(context.bot.messages), 1)
        self.assertEqual(context.bot.messages[0][0], 101)


if __name__ == "__main__":
    unittest.main()
