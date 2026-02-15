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

    async def register(self, payload: dict) -> dict:
        self._require_config()
        url = f"{self.base_url}?route=dl/geo_api/register&key={self.api_key}"
        data = await self._request("POST", url, json=payload)
        return data if isinstance(data, dict) else {"error": "Некорректный ответ API"}
