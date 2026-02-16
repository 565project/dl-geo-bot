import unittest
from urllib.parse import parse_qs

import httpx

from shiftbot.opencart_client import OpenCartClient


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class OpenCartClientPingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_add_drops_timestamp_fields(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(request.url.query.decode())
            captured["body"] = parse_qs(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        response = await client.ping_add(
            shift_id=100,
            staff_id=777,
            lat=43.222,
            lon=76.851,
            acc=12.0,
            status_fields={"source": "tg", "ping_at": "123", "timestamp": "456"},
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(captured["query"]["route"], ["dl/geo_api/ping_add"])
        self.assertEqual(captured["body"]["shift_id"], ["100"])
        self.assertEqual(captured["body"]["staff_id"], ["777"])
        self.assertEqual(captured["body"]["source"], ["tg"])
        self.assertNotIn("ping_at", captured["body"])
        self.assertNotIn("timestamp", captured["body"])

    async def test_violation_tick_handles_api_unavailable(self):
        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        self.addAsyncCleanup(client.aclose)

        async def fake_request(*args, **kwargs):
            from shiftbot.opencart_client import ApiUnavailableError

            raise ApiUnavailableError("temporary_api_error")

        client._request = fake_request
        response = await client.violation_tick(42)

        self.assertEqual(response.get("ok"), False)
        self.assertEqual(response.get("error"), "temporary_api_error")
        self.assertEqual(response.get("decisions"), {})


if __name__ == "__main__":
    unittest.main()
