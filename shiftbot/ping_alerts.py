import time

from telegram.ext import ContextTypes

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
        cluster = alert.get("dead_souls_cluster") if isinstance(alert.get("dead_souls_cluster"), dict) else {}
        cluster_point_id = cluster.get("point_id") or point_id
        point_name = cluster.get("point_name") or alert.get("point_name")
        point_text = f"{cluster_point_id}"
        if point_name:
            point_text = f"{cluster_point_id} ({point_name})"

        staff = cluster.get("staff") if isinstance(cluster.get("staff"), list) else []
        staff_lines = []
        for member in staff:
            if not isinstance(member, dict):
                continue
            member_name = member.get("full_name") or member.get("staff_name")
            member_id = member.get("staff_id") or "—"
            role = member.get("role")
            name_text = member_name or f"ID {member_id}"
            role_text = f" — {role}" if role else ""
            staff_lines.append(f"• {name_text}{role_text}")

        if not staff_lines:
            staff_lines.append(f"• {full_name or f'ID {staff_id}'}")

        staff_block = "\n".join(staff_lines)

        return (
            None,
            "⚠ Подозрительная гео-активность\n"
            f"Точка: {point_text}\n"
            "Сотрудники в кластере:\n"
            f"{staff_block}\n"
            "2 раза подряд отправлены одинаковые координаты.\n"
            "Требуется проверка (возможна фиксация с одного устройства).",
        )
    return None, None




def _admin_chat_ids_from_alert(alert: dict) -> list[int]:
    raw = alert.get("admin_chat_ids") if isinstance(alert, dict) else None
    if not isinstance(raw, list):
        return []
    values = []
    for chat_id in raw:
        value = _as_int(chat_id)
        if isinstance(value, int) and value > 0:
            values.append(value)
    return sorted(set(values))


def _admin_chat_ids_from_app(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    app = getattr(context, "application", None)
    if app is None:
        return []

    raw = app.bot_data.get("admin_chat_ids")
    if not isinstance(raw, list):
        return []

    values = []
    for chat_id in raw:
        value = _as_int(chat_id)
        if isinstance(value, int) and value > 0:
            values.append(value)
    return sorted(set(values))


async def process_ping_alerts(
    *,
    response: dict,
    context: ContextTypes.DEFAULT_TYPE,
    staff_chat_id: int,
    fallback_shift_id: int | None,
    logger,
) -> None:
    alerts = response.get("alerts") if isinstance(response, dict) else None
    normalized_alerts = [alert for alert in alerts if isinstance(alert, dict)] if isinstance(alerts, list) else []

    admin_alert = str(response.get("admin_alert") or "") if isinstance(response, dict) else ""
    if admin_alert == "admin_same_location_2":
        normalized_alerts.append(
            {
                "type": admin_alert,
                "shift_id": response.get("shift_id"),
                "point_id": response.get("point_id"),
                "point_name": response.get("point_name"),
                "dead_souls_cluster": response.get("dead_souls_cluster"),
                "admin_chat_ids": response.get("admin_chat_ids"),
            }
        )

    if not normalized_alerts:
        return

    now = time.time()
    cooldowns = context.application.bot_data.setdefault(PING_ALERT_COOLDOWN_KEY, {})
    for raw_alert in normalized_alerts:

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
            admin_chat_ids = _admin_chat_ids_from_alert(raw_alert)
            if not admin_chat_ids and alert_type.startswith("admin_same_location_"):
                admin_chat_ids = _admin_chat_ids_from_app(context)
            if not admin_chat_ids:
                logger.error("ADMIN_CHAT_IDS_EMPTY_FOR_ALERT shift_id=%s alert_type=%s", shift_id, alert_type)
                continue
            for admin_chat_id in admin_chat_ids:
                await context.bot.send_message(chat_id=admin_chat_id, text=admin_text)
            logger.info(
                "ADMIN_ALERT_SENT shift_id=%s alert_type=%s admin_chat_ids=%s",
                shift_id,
                alert_type,
                admin_chat_ids,
            )
            cooldowns[cooldown_key] = now
