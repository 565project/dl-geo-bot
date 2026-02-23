import unittest
from urllib.parse import parse_qs

import asyncio
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

    async def test_violation_tick_sends_json_payload(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(request.url.query.decode())
            captured["headers"] = dict(request.headers)
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True})

        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        response = await client.violation_tick(17)

        self.assertEqual(response, {"ok": True})
        self.assertEqual(captured["query"]["route"], ["dl/geo_api/violation_tick"])
        self.assertEqual(captured["headers"].get("content-type"), "application/json")
        self.assertEqual(captured["body"], '{"shift_id":17}')

    async def test_get_admin_chat_ids_retries_admin_endpoint_on_404(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path.endswith("/index.php") and not request.url.path.endswith("/admin/index.php"):
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json={"ok": True, "chat_ids": [101, "202"]})

        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        result = await client.get_admin_chat_ids()

        self.assertEqual(result, [101, 202])
        self.assertEqual(len(calls), 2)
        self.assertIn("/index.php", calls[0])
        self.assertIn("/admin/index.php", calls[1])

    async def test_get_admin_chat_ids_fallback_on_non_json_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        result = await client.get_admin_chat_ids()

        self.assertEqual(result, [783143356])

    async def test_health_check_logs_ok(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        result = await client.health_check()

        self.assertTrue(result)

    async def test_request_returns_structured_non_2xx_json(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_key"})

        client = OpenCartClient("https://example.com", "secret", DummyLogger())
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        result = await client._request("GET", params={"route": "x"})

        self.assertEqual(result["status"], 401)
        self.assertEqual(result["json"], {"error": "invalid_key"})



def test_init_rejects_admin_indexphp_in_base_url():
    with unittest.TestCase().assertRaises(ValueError):
        OpenCartClient("http://h:8080/admin/index.php", "secret", DummyLogger())

def test_build_url_strips_double_indexphp():
    cli = OpenCartClient("http://h:8080/index.php", "secret", DummyLogger())
    try:
        assert cli._build_url("admin/index.php") == "http://h:8080/admin/index.php"
    finally:
        asyncio.run(cli.aclose())




if __name__ == "__main__":
    unittest.main()
