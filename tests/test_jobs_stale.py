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
    def __init__(self, response=None, exc=None, staff=None, active_shift=None):
        self.response = response
        self.exc = exc
        self.staff = staff
        self.active_shift = active_shift
        self.calls = []
        self.staff_calls = []
        self.active_shift_calls = []

    async def violation_tick(self, shift_id: int):
        self.calls.append(shift_id)
        if self.exc:
            raise self.exc
        return self.response

    async def get_staff_by_telegram(self, user_id: int):
        self.staff_calls.append(user_id)
        return self.staff

    async def get_active_shift_by_staff(self, staff_id: int):
        self.active_shift_calls.append(staff_id)
        return self.active_shift


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
            },
            staff={"staff_id": 44},
            active_shift={"shift_id": 555},
        )

        context = SimpleNamespace(
            bot=DummyBot(),
            application=SimpleNamespace(bot_data={ADMIN_NOTIFY_COOLDOWN_KEY: {}, "admin_chat_ids": [9001]}),
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
        self.assertEqual(len(staff_msgs), 1)
        self.assertEqual(len(admin_msgs), 1)

    async def test_network_failure_on_tick_keeps_staff_warning_and_no_crash(self):
        now = time.time()
        session = ShiftSession(user_id=2, chat_id=101, active=True)
        session.active_shift_id = 777
        session.last_ping_ts = now - (config.STALE_AFTER_SEC + 5)

        oc_client = DummyOcClient(exc=RuntimeError("endpoint down"), staff={"staff_id": 66}, active_shift={"shift_id": 777})
        context = SimpleNamespace(
            bot=DummyBot(),
            application=SimpleNamespace(bot_data={ADMIN_NOTIFY_COOLDOWN_KEY: {}, "admin_chat_ids": []}),
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

    async def test_shift_not_active_stops_monitoring_without_admin_notify(self):
        now = time.time()
        session = ShiftSession(user_id=3, chat_id=102, active=True)
        session.active_shift_id = 15
        session.last_ping_ts = now - (config.STALE_AFTER_SEC + 5)
        session.last_active_shift_refresh_ts = now

        oc_client = DummyOcClient(
            response={"ok": False, "error": "shift_not_active", "shift_id": 15},
            staff={"staff_id": 77},
            active_shift=None,
        )
        context = SimpleNamespace(
            bot=DummyBot(),
            application=SimpleNamespace(bot_data={ADMIN_NOTIFY_COOLDOWN_KEY: {}, "admin_chat_ids": []}),
        )
        stale_job = build_job_check_stale(DummySessionStore([session]), oc_client, DummyLogger())

        original_notify_cd = config.STALE_NOTIFY_COOLDOWN_SEC
        config.STALE_NOTIFY_COOLDOWN_SEC = 0
        try:
            await stale_job(context)
        finally:
            config.STALE_NOTIFY_COOLDOWN_SEC = original_notify_cd

        self.assertEqual(oc_client.calls, [])
        self.assertFalse(session.active)
        self.assertIsNone(session.active_shift_id)
        self.assertEqual(len(context.bot.messages), 1)


if __name__ == "__main__":
    unittest.main()
