import os

# Тестовые значения (могут быть переопределены через env)
BOT_TOKEN_DEFAULT = "8105246434:AAH-6lBOMulCmgGoKlsFNVNftV6mYRh8K1Q"
OC_API_BASE_DEFAULT = "http://100.121.213.12:8080/index.php"
OC_API_KEY_DEFAULT = "fd34635cd94a789b50dfce3373b0ba78"

BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN_DEFAULT)
OC_API_BASE = os.getenv("OC_API_BASE", OC_API_BASE_DEFAULT)
OC_API_KEY = os.getenv("OC_API_KEY", OC_API_KEY_DEFAULT)

POINT_LAT = float(os.getenv("POINT_LAT", "56.628495"))
POINT_LON = float(os.getenv("POINT_LON", "47.894357"))

DEFAULT_RADIUS_M = int(os.getenv("DEFAULT_RADIUS_M", os.getenv("RADIUS_M", "120")))
ACCURACY_MAX_M = int(os.getenv("ACCURACY_MAX_M", "60"))
GATE_MAX_ATTEMPTS = int(os.getenv("GATE_MAX_ATTEMPTS", "5"))
GATE_RADIUS_STEP_M = int(os.getenv("GATE_RADIUS_STEP_M", "10"))

OUT_LIMIT = int(os.getenv("OUT_LIMIT", "3"))


def _parse_admin_phones(raw: str) -> list[str]:
    phones = [item.strip() for item in raw.split(",") if item.strip()]
    return phones or ["89033262408"]


def _parse_admin_chat_ids(raw: str) -> list[int]:
    chat_ids: list[int] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            chat_id = int(value)
        except ValueError:
            continue
        if chat_id > 0:
            chat_ids.append(chat_id)
    return sorted(set(chat_ids))


ADMIN_PHONE = os.getenv("ADMIN_PHONE", "89033262408")
ADMIN_PHONES = _parse_admin_phones(os.getenv("ADMIN_PHONES", ADMIN_PHONE))
ADMIN_CHAT_IDS = _parse_admin_chat_ids(os.getenv("ADMIN_CHAT_IDS", ""))
# Жёсткий админ для теста
ADMIN_FORCE_CHAT_IDS = [783143356]
DEAD_SOUL_STREAK = int(os.getenv("DEAD_SOUL_STREAK", "10"))
DEAD_SOUL_BUCKET_SEC = int(os.getenv("DEAD_SOUL_BUCKET_SEC", "10"))
DEAD_SOUL_WINDOW_SEC = int(os.getenv("DEAD_SOUL_WINDOW_SEC", "25"))
GPS_SIG_ROUND = int(os.getenv("GPS_SIG_ROUND", "5"))
ALERT_COOLDOWN_OUT_SEC = int(os.getenv("ALERT_COOLDOWN_OUT_SEC", "300"))
ALERT_COOLDOWN_DEAD_SEC = int(os.getenv("ALERT_COOLDOWN_DEAD_SEC", "900"))

OUT_COOLDOWN_SEC = ALERT_COOLDOWN_OUT_SEC

OUT_STREAK_ALERT = int(os.getenv("OUT_STREAK_ALERT", "3"))
SAME_GPS_STREAK_ALERT = int(os.getenv("SAME_GPS_STREAK_ALERT", "20"))
GPS_BUCKET_SEC = int(os.getenv("GPS_BUCKET_SEC", "30"))
NOTIFY_COOLDOWN_SEC = int(os.getenv("NOTIFY_COOLDOWN_SEC", "60"))
DEBUG_GPS_NOTIFY = os.getenv("DEBUG_GPS_NOTIFY", "0") in {"1", "true", "True"}
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

# Временный жёсткий канал тестовых admin-уведомлений
HARD_ADMIN_CHAT_ID = 8221611228
HARD_ADMIN_ENABLED = False
HARD_ADMIN_DELAY_SEC = 1

ENABLE_STALE_CHECK = os.getenv("ENABLE_STALE_CHECK", "1") not in {"0", "false", "False"}
STALE_CHECK_EVERY_SEC = int(os.getenv("STALE_CHECK_EVERY_SEC", "30"))
STALE_AFTER_SEC = int(os.getenv("STALE_AFTER_SEC", "90"))
STALE_NOTIFY_COOLDOWN_SEC = int(os.getenv("STALE_NOTIFY_COOLDOWN_SEC", "180"))
ADMIN_NOTIFY_COOLDOWN_SEC = int(os.getenv("ADMIN_NOTIFY_COOLDOWN_SEC", "300"))
PING_NOTIFY_EVERY_SEC = int(os.getenv("PING_NOTIFY_EVERY_SEC", "15"))

STAFF_CACHE_TTL_SEC = int(os.getenv("STAFF_CACHE_TTL_SEC", "30"))
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC", "10"))

REG_NAME, REG_CONTACT, REG_TYPE = range(3)
