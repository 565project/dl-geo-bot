from typing import Optional

import httpx

from shiftbot import config


class OpenCartClient:
    def __init__(self, base_url: str, api_key: str, logger) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.logger = logger

    def _require_config(self) -> None:
        if not self.base_url or not self.api_key:
            raise RuntimeError("OC_API_BASE/OC_API_KEY не заданы.")

    async def _request(self, method: str, url: str, *, json: Optional[dict] = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SEC) as client:
                response = await client.request(method, url, json=json)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.logger.exception("API_ERROR method=%s url=%s error=%s", method, url, exc)
            raise RuntimeError("temporary_api_error") from exc
        return payload if isinstance(payload, dict) else {}

    async def get_staff(self, telegram_user_id: int) -> Optional[dict]:
        self._require_config()
        url = (
            f"{self.base_url}?route=dl/geo_api/staff_by_telegram"
            f"&key={self.api_key}&telegram_user_id={telegram_user_id}"
        )
        payload = await self._request("GET", url)
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def staff_by_phone(self, phone: str) -> Optional[dict]:
        self._require_config()
        url = (
            f"{self.base_url}?route=dl/geo_api/staff_by_phone"
            f"&key={self.api_key}&phone={phone}"
        )
        payload = await self._request("GET", url)
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_staff_by_phone(self, phone_raw: str) -> Optional[dict]:
        self._require_config()
        url = (
            f"{self.base_url}?route=dl/geo_api/staff_by_phone"
            f"&key={self.api_key}&phone={phone_raw}"
        )
        payload = await self._request("GET", url)
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_points(self) -> list[dict]:
        self._require_config()
        url = f"{self.base_url}?route=dl/geo_api/points&key={self.api_key}"
        payload = await self._request("GET", url)
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
        url = f"{self.base_url}?route=dl/geo_api/shift_start&key={self.api_key}"
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

        try:
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SEC) as client:
                response = await client.post(url, data=clean_payload, timeout=config.HTTP_TIMEOUT_SEC)
        except httpx.HTTPError as exc:
            self.logger.exception("API_ERROR method=POST url=%s error=%s", url, exc)
            raise RuntimeError("temporary_api_error") from exc

        try:
            data = response.json()
        except Exception:
            data = None

        if response.status_code >= 400:
            self.logger.warning(
                "SHIFT_START_%s json=%s text=%s",
                response.status_code,
                data,
                response.text[:400],
            )
            print(
                f"SHIFT_START_{response.status_code} json={data} text={response.text[:400]}",
                flush=True,
            )
            return {
                "success": False,
                "status": response.status_code,
                "json": data,
                "text": response.text,
            }

        return data

    async def shift_end(self, payload: dict) -> dict:
        self._require_config()
        url = f"{self.base_url}?route=dl/geo_api/shift_end&key={self.api_key}"
        data = await self._request("POST", url, json=payload)
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def rebind_telegram(
        self,
        staff_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        mode: str,
    ) -> dict:
        self._require_config()
        url = f"{self.base_url}?route=dl/geo_api/rebind_telegram&key={self.api_key}"
        payload = {
            "staff_id": staff_id,
            "telegram_user_id": str(telegram_user_id),
            "telegram_chat_id": str(telegram_chat_id),
            "mode": mode,
        }
        data = await self._request("POST", url, json=payload)
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def register(self, payload: dict) -> dict:
        self._require_config()
        url = f"{self.base_url}?route=dl/geo_api/register&key={self.api_key}"
        data = await self._request("POST", url, json=payload)
        return data if isinstance(data, dict) else {"error": "Некорректный ответ API"}
