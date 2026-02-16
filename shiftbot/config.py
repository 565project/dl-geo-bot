import os

# Тестовые значения (могут быть переопределены через env)
BOT_TOKEN_DEFAULT = "8105246434:AAH-6lBOMulCmgGoKlsFNVNftV6mYRh8K1Q"
OC_API_BASE_DEFAULT = "https://dobrolunch-analitika.mywebcenter.ru/index.php"
OC_API_KEY_DEFAULT = "fd34635cd94a789b50dfce3373b0ba78"

BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN_DEFAULT)
OC_API_BASE = os.getenv("OC_API_BASE", OC_API_BASE_DEFAULT)
OC_API_KEY = os.getenv("OC_API_KEY", OC_API_KEY_DEFAULT)

POINT_LAT = float(os.getenv("POINT_LAT", "56.628495"))
POINT_LON = float(os.getenv("POINT_LON", "47.894357"))

DEFAULT_RADIUS_M = int(os.getenv("DEFAULT_RADIUS_M", os.getenv("RADIUS_M", "120")))
ACCURACY_MAX_M = int(os.getenv("ACCURACY_MAX_M", "60"))

OUT_LIMIT = int(os.getenv("OUT_LIMIT", "3"))
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "89033262408")
OUT_COOLDOWN_SEC = int(os.getenv("OUT_COOLDOWN_SEC", "300"))

OUT_STREAK_REQUIRED = int(os.getenv("OUT_STREAK_REQUIRED", "2"))
WARN_COOLDOWN_SEC = int(os.getenv("WARN_COOLDOWN_SEC", "120"))

ENABLE_STALE_CHECK = os.getenv("ENABLE_STALE_CHECK", "1") not in {"0", "false", "False"}
STALE_CHECK_EVERY_SEC = int(os.getenv("STALE_CHECK_EVERY_SEC", "30"))
STALE_AFTER_SEC = int(os.getenv("STALE_AFTER_SEC", "90"))
STALE_NOTIFY_COOLDOWN_SEC = int(os.getenv("STALE_NOTIFY_COOLDOWN_SEC", "180"))

STAFF_CACHE_TTL_SEC = int(os.getenv("STAFF_CACHE_TTL_SEC", "30"))
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC", "10"))

REG_NAME, REG_CONTACT, REG_TYPE = range(3)
