import time

from telegram.ext import ContextTypes

from shiftbot import config

PING_ALERT_COOLDOWN_KEY = "ping_alert_cooldowns"
STAFF_ALERT_COOLDOWN_SEC = 120
DEFAULT_ALERT_COOLDOWN_SEC = 300


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _alert_cooldown_sec(alert_type: str) -> int:
    if alert_type in {"staff_out_of_zone_warn", "admin_same_location_2"}:
        return STAFF_ALERT_COOLDOWN_SEC
    return DEFAULT_ALERT_COOLDOWN_SEC


def _alert_text(alert: dict) -> tuple[str | None, str | None]:
    alert_type = str(alert.get("type") or "")
    shift_id = alert.get("shift_id") or "—"
    staff_id = alert.get("staff_id") or "—"
    full_name = alert.get("full_name")
    point_id = alert.get("point_id") or "—"

    if alert_type == "staff_out_of_zone_warn":
        return (
            "Вы вне рабочей зоны. Вернитесь в зону, включите GPS или сообщите руководителю об ошибке.",
            None,
        )
    if alert_type == "admin_suspicious_out_round1":
        return (
            None,
            "Подозрительная активность: сотрудник "
            f"{staff_id} вне зоны 3 раза подряд. Точка {point_id}, смена {shift_id}. "
            "Ждём повтор для проверки.",
        )
    if alert_type == "admin_escalate_out_round2":
        return (
            None,
            "ЭСКАЛАЦИЯ: сотрудник "
            f"{staff_id} вне зоны второй раз (ещё 3 подряд). "
            f"Запросить проверку. Точка {point_id}, смена {shift_id}.",
        )
    if alert_type == "admin_same_location_10":
        return (
            None,
            "Подозрительная активность: одинаковые координаты 10 раз подряд. "
            f"Сотрудник {staff_id}, точка {point_id}, смена {shift_id}. "
            "Срочно запросить фотоподтверждение.",
        )
    if alert_type == "admin_same_location_2":
        return (
            None,
            "⚠ Подозрительная гео-активность\n"
            f"Сотрудник: {full_name or staff_id}\n"
            f"Точка: {point_id}\n"
            "2 раза подряд отправлены одинаковые координаты.\n"
            "Требуется проверка (возможна фиксация с одного устройства).",
        )
    return None, None


def _admin_chat_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    raw = context.application.bot_data.get("admin_chat_ids") if context and context.application else None
    if isinstance(raw, list):
        values = [int(chat_id) for chat_id in raw if isinstance(chat_id, int) and chat_id > 0]
        if values:
            return sorted(set(values))

    values = list(config.ADMIN_CHAT_IDS)
    if config.ADMIN_CHAT_ID > 0:
        values.append(config.ADMIN_CHAT_ID)
    return sorted(set(values))


async def process_ping_alerts(
    *,
    response: dict,
    context: ContextTypes.DEFAULT_TYPE,
    staff_chat_id: int,
    fallback_shift_id: int | None,
    logger,
) -> None:
    alerts = response.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        return

    now = time.time()
    cooldowns = context.application.bot_data.setdefault(PING_ALERT_COOLDOWN_KEY, {})
    admin_chat_ids = _admin_chat_ids(context)

    for raw_alert in alerts:
        if not isinstance(raw_alert, dict):
            continue

        alert_type = str(raw_alert.get("type") or "")
        if not alert_type:
            continue

        shift_id = _as_int(raw_alert.get("shift_id"))
        if shift_id is None:
            shift_id = fallback_shift_id
        cooldown_key = (alert_type, shift_id)
        cooldown_sec = _alert_cooldown_sec(alert_type)

        last_sent_at = cooldowns.get(cooldown_key)
        if isinstance(last_sent_at, (int, float)) and (now - float(last_sent_at)) < cooldown_sec:
            continue

        staff_text, admin_text = _alert_text(raw_alert)
        if not staff_text and not admin_text:
            continue

        if staff_text:
            await context.bot.send_message(chat_id=staff_chat_id, text=staff_text)
            cooldowns[cooldown_key] = now

        if admin_text:
            if not admin_chat_ids:
                logger.warning("ADMIN_CHAT_IDS_NOT_SET")
                continue
            for admin_chat_id in admin_chat_ids:
                await context.bot.send_message(chat_id=admin_chat_id, text=admin_text)
            cooldowns[cooldown_key] = now
