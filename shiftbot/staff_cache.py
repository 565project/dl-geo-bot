import time
from typing import Dict, Optional, Tuple


class StaffCache:
    def __init__(self, ttl_sec: int = 30) -> None:
        self.ttl_sec = ttl_sec
        self._cache: Dict[int, Tuple[float, Optional[dict]]] = {}

    def get(self, telegram_user_id: int) -> Tuple[bool, Optional[dict]]:
        item = self._cache.get(telegram_user_id)
        if not item:
            return False, None
        ts, staff = item
        if (time.time() - ts) > self.ttl_sec:
            self._cache.pop(telegram_user_id, None)
            return False, None
        return True, staff

    def set(self, telegram_user_id: int, staff: Optional[dict]) -> None:
        self._cache[telegram_user_id] = (time.time(), staff)

    def invalidate(self, telegram_user_id: int) -> None:
        self._cache.pop(telegram_user_id, None)
