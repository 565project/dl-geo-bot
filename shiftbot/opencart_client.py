from typing import Optional
import asyncio

import httpx


class ApiUnavailableError(RuntimeError):
    pass


class OpenCartClient:
    def __init__(self, base_url: str, api_key: str, logger) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.logger = logger
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
            headers={"User-Agent": "dl-geo-bot/1.0"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _require_config(self) -> None:
        if not self.base_url or not self.api_key:
            raise RuntimeError("OC_API_BASE/OC_API_KEY не заданы.")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> dict:
        network_backoff = [0.3, 0.8, 1.8]
        status_backoff = [0.3, 0.8]
        network_errors = (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        )

        for attempt in range(1, len(network_backoff) + 2):
            self.logger.info(
                "API_REQUEST attempt=%s method=%s url=%s params=%s has_data=%s has_json=%s",
                attempt,
                method,
                url,
                params,
                data is not None,
                json is not None,
            )
            try:
                response = await self._client.request(method, url, params=params, data=data, json=json)
            except network_errors as exc:
                self.logger.warning(
                    "API_REQUEST_EXCEPTION attempt=%s method=%s url=%s error_type=%s error=%s",
                    attempt,
                    method,
                    url,
                    type(exc).__name__,
                    exc,
                )
                if attempt <= len(network_backoff):
                    await asyncio.sleep(network_backoff[attempt - 1])
                    continue
                raise ApiUnavailableError("temporary_api_error") from exc
            except httpx.HTTPError as exc:
                self.logger.exception("API_ERROR method=%s url=%s error=%s", method, url, exc)
                raise ApiUnavailableError("temporary_api_error") from exc

            if response.status_code in {502, 503, 504}:
                if attempt <= len(status_backoff):
                    self.logger.warning(
                        "API_REQUEST_RETRY_STATUS attempt=%s method=%s url=%s status=%s",
                        attempt,
                        method,
                        url,
                        response.status_code,
                    )
                    await asyncio.sleep(status_backoff[attempt - 1])
                    continue
                raise ApiUnavailableError(f"temporary_api_error status={response.status_code}")

            if response.status_code >= 500:
                self.logger.error(
                    "API_ERROR_STATUS method=%s url=%s status=%s",
                    method,
                    url,
                    response.status_code,
                )
                raise ApiUnavailableError(f"temporary_api_error status={response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                self.logger.exception("API_ERROR_JSON method=%s url=%s error=%s", method, url, exc)
                raise ApiUnavailableError("temporary_api_error") from exc

            if response.status_code >= 400:
                return {
                    "success": False,
                    "status": response.status_code,
                    "json": payload if isinstance(payload, dict) else None,
                    "text": response.text,
                }

            return payload if isinstance(payload, dict) else {}

        raise ApiUnavailableError("temporary_api_error")

    async def get_staff(self, telegram_user_id: int) -> Optional[dict]:
        self._require_config()
        url = f"{self.base_url}"
        payload = await self._request(
            "GET",
            url,
            params={
                "route": "dl/geo_api/staff_by_telegram",
                "key": self.api_key,
                "telegram_user_id": telegram_user_id,
            },
        )
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def staff_by_phone(self, phone: str) -> Optional[dict]:
        self._require_config()
        url = f"{self.base_url}"
        payload = await self._request(
            "GET",
            url,
            params={"route": "dl/geo_api/staff_by_phone", "key": self.api_key, "phone": phone},
        )
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_staff_by_phone(self, phone_raw: str) -> Optional[dict]:
        self._require_config()
        url = f"{self.base_url}"
        payload = await self._request(
            "GET",
            url,
            params={"route": "dl/geo_api/staff_by_phone", "key": self.api_key, "phone": phone_raw},
        )
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_points(self) -> list[dict]:
        self._require_config()
        url = f"{self.base_url}"
        payload = await self._request("GET", url, params={"route": "dl/geo_api/points", "key": self.api_key})
        points = payload.get("points")
        if not isinstance(points, list):
            return []

        normalized_points: list[dict] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            item = dict(point)
            item["geo_lat"] = point.get("geo_lat")
            item["geo_lon"] = point.get("geo_lon") or point.get("geo_lng") or point.get("geo_long")
            item["geo_radius_m"] = point.get("geo_radius_m") or point.get("radius") or point.get("geo_radius")
            normalized_points.append(item)
        return normalized_points

    async def shift_start(self, payload: dict) -> dict | None:
        self._require_config()
        url = f"{self.base_url}"
        clean_payload = {
            "staff_id": str(payload.get("staff_id")),
            "point_id": str(payload.get("point_id")),
            "role": str(payload.get("role")),
            "start_lat": str(payload.get("start_lat")),
            "start_lon": str(payload.get("start_lon")),
        }
        if payload.get("start_acc") is not None:
            clean_payload["start_acc"] = str(int(payload.get("start_acc")))

        self.logger.info("SHIFT_START payload=%s", clean_payload)
        print(f"SHIFT_START payload={clean_payload}", flush=True)

        return await self._request(
            "POST",
            url,
            params={"route": "dl/geo_api/shift_start", "key": self.api_key},
            data=clean_payload,
        )

    async def shift_end(self, payload: dict) -> dict:
        self._require_config()
        url = f"{self.base_url}"
        data = await self._request(
            "POST",
            url,
            params={"route": "dl/geo_api/shift_end", "key": self.api_key},
            json=payload,
        )
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def rebind_telegram(
        self,
        staff_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        mode: str,
    ) -> dict:
        self._require_config()
        url = f"{self.base_url}"
        payload = {
            "staff_id": staff_id,
            "telegram_user_id": str(telegram_user_id),
            "telegram_chat_id": str(telegram_chat_id),
            "mode": mode,
        }
        data = await self._request(
            "POST",
            url,
            params={"route": "dl/geo_api/rebind_telegram", "key": self.api_key},
            json=payload,
        )
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def register(self, payload: dict) -> dict:
        self._require_config()
        url = f"{self.base_url}"
        data = await self._request(
            "POST",
            url,
            params={"route": "dl/geo_api/register", "key": self.api_key},
            json=payload,
        )
        return data if isinstance(data, dict) else {"error": "Некорректный ответ API"}
