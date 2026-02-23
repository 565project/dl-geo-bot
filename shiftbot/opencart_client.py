from typing import Optional
import asyncio
import json
import time

import httpx


class ApiUnavailableError(RuntimeError):
    pass


class OpenCartClient:
    def __init__(self, base_url: str, api_key: str, logger, admin_base_url: str | None = None) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.admin_base_url = self._normalize_base_url(admin_base_url) if admin_base_url else None
        self.api_key = api_key
        self.logger = logger
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
            headers={"User-Agent": "dl-geo-bot/1.0"},
            follow_redirects=True,
        )
        self._admin_chat_ids_cache: list[int] | None = None
        self._admin_chat_ids_cache_ts: float = 0.0

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = str(base_url or "").rstrip("/")
        if normalized.endswith("/admin/index.php"):
            raise ValueError("base_url must not include '/admin/index.php'; use base domain/path only")
        if normalized.endswith("/index.php"):
            normalized = normalized[: -len("/index.php")]
        return normalized

    def _build_url(self, endpoint_path: str) -> str:
        base = self.base_url
        if base.endswith("/index.php"):
            base = base[: -len("/index.php")]
        endpoint = endpoint_path.lstrip("/")
        return f"{base.rstrip('/')}/{endpoint}"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _require_config(self) -> None:
        if not self.base_url or not self.api_key:
            raise RuntimeError("OC_API_BASE/OC_API_KEY не заданы.")

    async def _request(
        self,
        method: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        json_data: Optional[dict] = None,
        headers: Optional[dict] = None,
        *,
        endpoint_path: str = "index.php",
        return_meta: bool = False,
    ) -> dict:
        self._require_config()
        url = self._build_url(endpoint_path)

        all_params = dict(params or {})
        all_params["key"] = self.api_key

        network_backoff = [0.3, 0.8, 1.8]
        status_backoff = [0.3, 0.8]
        network_errors = (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        )

        for attempt in range(1, len(network_backoff) + 2):
            self.logger.info(
                "API_REQUEST attempt=%s method=%s params=%s has_data=%s",
                attempt,
                method,
                all_params,
                (data is not None or json_data is not None),
            )
            try:
                if method.upper() == "POST":
                    response = await self._client.request(
                        method,
                        url,
                        params=all_params,
                        data=data,
                        json=json_data,
                        headers=headers,
                    )
                else:
                    response = await self._client.request(method, url, params=all_params)
            except network_errors as exc:
                self.logger.warning(
                    "API_REQUEST_EXCEPTION attempt=%s method=%s error_type=%s attempt_no=%s error=%s",
                    attempt,
                    method,
                    type(exc).__name__,
                    attempt,
                    exc,
                )
                if attempt <= len(network_backoff):
                    await asyncio.sleep(network_backoff[attempt - 1])
                    continue
                raise ApiUnavailableError("temporary_api_error") from exc
            except httpx.HTTPError as exc:
                self.logger.warning(
                    "API_REQUEST_EXCEPTION attempt=%s method=%s error_type=%s attempt_no=%s error=%s",
                    attempt,
                    method,
                    type(exc).__name__,
                    attempt,
                    exc,
                )
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

            if response.status_code >= 400:
                payload = None
                try:
                    parsed = response.json()
                    payload = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    payload = None
                self.logger.warning(
                    "API_NON_2XX method=%s url=%s status=%s body=%s",
                    method,
                    url,
                    response.status_code,
                    response.text[:300],
                )
                return {
                    "success": False,
                    "status": response.status_code,
                    "json": payload,
                    "text": response.text,
                }

            try:
                payload = response.json()
            except ValueError as exc:
                self.logger.exception("API_ERROR_JSON method=%s url=%s error=%s", method, url, exc)
                raise ApiUnavailableError("temporary_api_error") from exc

            if return_meta:
                return {
                    "ok": True,
                    "status": response.status_code,
                    "json": payload if isinstance(payload, dict) else {},
                    "text": response.text,
                }

            return payload if isinstance(payload, dict) else {}

        raise ApiUnavailableError("temporary_api_error")

    async def get_staff(self, telegram_user_id: int) -> Optional[dict]:
        payload = await self._request(
            "GET",
            params={
                "route": "dl/geo_api/staff_by_telegram",
                "telegram_user_id": telegram_user_id,
            },
        )
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_staff_by_telegram(self, telegram_user_id: int) -> Optional[dict]:
        return await self.get_staff(telegram_user_id)

    async def staff_by_phone(self, phone: str) -> Optional[dict]:
        payload = await self._request(
            "GET",
            params={"route": "dl/geo_api/staff_by_phone", "phone": phone},
        )
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_staff_by_phone(self, phone_raw: str) -> Optional[dict]:
        payload = await self._request(
            "GET",
            params={"route": "dl/geo_api/staff_by_phone", "phone": phone_raw},
        )
        staff = payload.get("staff") if isinstance(payload, dict) else None
        return staff if isinstance(staff, dict) else None

    async def get_points(self) -> list[dict]:
        payload = await self._request("GET", params={"route": "dl/geo_api/points"})
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
            params={"route": "dl/geo_api/shift_start"},
            data=clean_payload,
        )

    async def shift_end(self, payload: dict) -> dict:
        clean_payload = {
            "shift_id": str(payload.get("shift_id")),
            "end_reason": str(payload.get("end_reason") or payload.get("reason") or "manual"),
        }
        data = await self._request(
            "POST",
            params={"route": "dl/geo_api/shift_end"},
            data=clean_payload,
        )
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def ping_add(
        self,
        *,
        shift_id: int,
        staff_id: int | None = None,
        telegram_id: int | None = None,
        lat: float,
        lon: float,
        acc: float | None = None,
        status_fields: Optional[dict] = None,
    ) -> dict:
        payload = {"shift_id": str(shift_id), "lat": str(lat), "lon": str(lon)}
        if staff_id is not None:
            payload["staff_id"] = str(staff_id)
        if telegram_id is not None:
            payload["telegram_id"] = str(telegram_id)
        if acc is not None:
            payload["acc"] = str(acc)

        for key, value in (status_fields or {}).items():
            if key in {"ping_at", "timestamp"}:
                continue
            if value is None:
                continue
            payload[str(key)] = str(value)

        data = await self._request(
            "POST",
            params={"route": "dl/geo_api/ping_add"},
            data=payload,
        )
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def ping_add_meta(self, payload: dict) -> dict:
        clean_payload = {str(key): str(value) for key, value in payload.items() if value is not None}
        clean_payload.pop("ping_at", None)
        clean_payload.pop("timestamp", None)
        data = await self._request(
            "POST",
            params={"route": "dl/geo_api/ping_add"},
            data=clean_payload,
            return_meta=True,
        )
        return data if isinstance(data, dict) else {"ok": False, "status": 0, "json": {}, "text": ""}

    async def get_active_shift_by_staff(self, staff_id: int) -> dict | None:
        payload = await self._request(
            "GET",
            params={"route": "dl/geo_api/active_shift_by_staff", "staff_id": str(staff_id)},
        )
        shift = payload.get("shift") if isinstance(payload, dict) else None
        return shift if isinstance(shift, dict) else None

    async def rebind_telegram(
        self,
        staff_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        mode: str,
    ) -> dict:
        payload = {
            "staff_id": staff_id,
            "telegram_user_id": str(telegram_user_id),
            "telegram_chat_id": str(telegram_chat_id),
            "mode": mode,
        }
        data = await self._request(
            "POST",
            params={"route": "dl/geo_api/rebind_telegram"},
            data=payload,
        )
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API"}

    async def register(self, payload: dict) -> dict:
        data = await self._request(
            "POST",
            params={"route": "dl/geo_api/register"},
            data=payload,
        )
        return data if isinstance(data, dict) else {"error": "Некорректный ответ API"}

    async def get_admin_chat_ids(self) -> list[int]:
        """Fetch admin chat IDs from API. Caches result for 60 seconds.
        Falls back to ADMIN_FORCE_CHAT_IDS from config on any failure."""
        from shiftbot import config

        now = time.time()
        if self._admin_chat_ids_cache is not None and (now - self._admin_chat_ids_cache_ts) < 600:
            return list(self._admin_chat_ids_cache)

        payload = await self._request(
            "GET",
            params={"route": "dl/geo_api", "action": "admin_chat_ids"},
            return_meta=True,
        )
        if payload.get("status") == 404:
            self.logger.info("ADMIN_CHAT_IDS_RETRY_ADMIN_ENDPOINT status=404")
            payload = await self._request(
                "GET",
                params={"route": "dl/geo_api", "action": "admin_chat_ids"},
                endpoint_path="admin/index.php",
                return_meta=True,
            )

        body = payload.get("json") if isinstance(payload, dict) else None
        if isinstance(body, dict) and body.get("ok") and isinstance(body.get("chat_ids"), list):
            result: list[int] = []
            for x in body["chat_ids"]:
                try:
                    v = int(x)
                    if v > 0:
                        result.append(v)
                except (TypeError, ValueError):
                    pass
            self._admin_chat_ids_cache = result
            self._admin_chat_ids_cache_ts = now
            self.logger.info("ADMIN_CHAT_IDS_FETCHED chat_ids=%s", result)
            return list(result)

        self.logger.warning("ADMIN_CHAT_IDS_FALLBACK status=%s payload=%s", payload.get("status"), body)

        return list(config.ADMIN_FORCE_CHAT_IDS)

    async def health_check(self) -> bool:
        payload = await self._request(
            "GET",
            params={"route": "dl/geo_api", "action": "ping"},
            return_meta=True,
        )
        status = int(payload.get("status") or 0)
        ok = 200 <= status < 300
        metric = "oc_api_health_ok" if ok else "oc_api_health_fail"
        self.logger.info("OC_API_HEALTH_CHECK metric=%s status=%s body=%s", metric, status, payload.get("json"))
        return ok

    async def get_active_shifts_by_point(self, point_id: int) -> list[dict]:
        """Fetch all active shifts at a given point."""
        try:
            payload = await self._request(
                "GET",
                params={"route": "dl/geo_api/active_shifts_by_point", "point_id": str(point_id)},
            )
        except Exception as exc:
            self.logger.warning("GET_ACTIVE_SHIFTS_BY_POINT_FAILED point_id=%s error=%s", point_id, exc)
            return []
        shifts = payload.get("shifts") if isinstance(payload, dict) else None
        if not isinstance(shifts, list):
            return []
        return [s for s in shifts if isinstance(s, dict)]

    async def violation_tick(self, shift_id: int) -> dict:
        payload = {"shift_id": int(shift_id)}
        payload_json = json.dumps(payload, ensure_ascii=False)
        request_headers = {"Content-Type": "application/json"}
        self.logger.info("VIOLATION_TICK_REQUEST payload=%s", payload)
        self.logger.info("VIOLATION_TICK_REQUEST_JSON body=%s", payload_json)
        self.logger.info("VIOLATION_TICK_REQUEST_HEADERS headers=%s", request_headers)
        try:
            data = await self._request(
                "POST",
                params={"route": "dl/geo_api/violation_tick"},
                json_data=payload,
                headers=request_headers,
            )
        except ApiUnavailableError as exc:
            self.logger.warning("VIOLATION_TICK_UNAVAILABLE shift_id=%s error=%s", shift_id, exc)
            return {"ok": False, "error": "temporary_api_error", "decisions": {}}
        return data if isinstance(data, dict) else {"ok": False, "error": "Некорректный ответ API", "decisions": {}}
