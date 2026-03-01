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
    def __init__(self):
        self.shift_end_calls = []
        self.staff_calls = []
        self.active_shift_calls = []
        self.violation_tick_calls = []
        self._shift_end_responses = [
            {"success": False, "status": 400, "json": {"success": False, "error": "bad_end_reason"}},
            {"success": True},
        ]

    async def get_staff_by_telegram(self, user_id: int):
        self.staff_calls.append(user_id)
        return {"staff_id": 1}

    async def get_active_shift_by_staff(self, staff_id: int):
        self.active_shift_calls.append(staff_id)
        # After successful stop we expect the server to return no active shift.
        if len(self.shift_end_calls) >= 2:
            return None
        return {"shift_id": 118}

    async def shift_end(self, payload: dict):
        self.shift_end_calls.append(payload)
        return self._shift_end_responses.pop(0)

    async def violation_tick(self, shift_id: int):
        self.violation_tick_calls.append(shift_id)
        return {"ok": True, "decisions": {}}


class StaleAutoStopReasonFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_round_retries_bad_end_reason(self):
        now = time.time()
        session = ShiftSession(user_id=10, chat_id=1010, active=True)
        session.active_shift_id = 118
        session.active_staff_name = "Тестовый"
        session.active_point_id = 30
        session.last_ping_ts = now - (config.STALE_AFTER_SEC + 5)

        context = SimpleNamespace(
            bot=DummyBot(),
            application=SimpleNamespace(bot_data={ADMIN_NOTIFY_COOLDOWN_KEY: {}, "admin_chat_ids": []}),
        )
        oc_client = DummyOcClient()
        stale_job = build_job_check_stale(DummySessionStore([session]), oc_client, DummyLogger())

        original_notify_cd = config.STALE_NOTIFY_COOLDOWN_SEC
        config.STALE_NOTIFY_COOLDOWN_SEC = 0
        try:
            # Round 1: warn user.
            await stale_job(context)
            # Round 2: first reason rejected with bad_end_reason, fallback reason succeeds.
            await stale_job(context)
        finally:
            config.STALE_NOTIFY_COOLDOWN_SEC = original_notify_cd

        self.assertEqual(len(oc_client.shift_end_calls), 2)
        self.assertEqual(oc_client.shift_end_calls[0]["end_reason"], "auto_stale_no_geo_second_notice")
        self.assertEqual(oc_client.shift_end_calls[1]["end_reason"], "auto_violation_out")
        self.assertFalse(session.active)
        self.assertIsNone(session.active_shift_id)

        self.assertEqual(oc_client.violation_tick_calls, [118, 118])



if __name__ == "__main__":
    unittest.main()
