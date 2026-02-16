import time
from datetime import datetime

from telegram.ext import ContextTypes

from shiftbot import config

ADMIN_NOTIFY_COOLDOWN_KEY = "admin_notify_cooldowns"


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _admin_chat_ids_from_response(response: dict) -> list[int]:
    raw = response.get("admin_chat_ids") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return []
    values = [_as_int(chat_id) for chat_id in raw]
    return sorted({chat_id for chat_id in values if isinstance(chat_id, int) and chat_id > 0})


def _format_last_seen(last_ping_ts: float) -> str:
    if not isinstance(last_ping_ts, (int, float)) or last_ping_ts <= 0:
        return "—"
    return datetime.fromtimestamp(last_ping_ts).strftime("%Y-%m-%d %H:%M:%S")


async def maybe_send_admin_notify_from_decision(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    response: dict,
    shift_id: int,
    logger,
    staff_name: str | None = None,
    point_id: int | None = None,
    last_ping_ts: float | None = None,
    cooldown_reason: str | None = None,
    cooldown_min_sec: int = 0,
    text: str | None = None,
) -> bool:
    if not isinstance(response, dict) or not response.get("ok"):
        return False

    decisions = response.get("decisions")
    if not isinstance(decisions, dict) or not decisions.get("admin_notify"):
        return False

    reason = str(response.get("reason") or "UNKNOWN")
    round_value = _as_int(response.get("round"))

    app = context.application if context else None
    if app is None:
        return False

    cooldowns = app.bot_data.setdefault(ADMIN_NOTIFY_COOLDOWN_KEY, {})
    now = time.time()
    cooldown_reason_value = str(cooldown_reason or reason)
    cooldown_key = (int(shift_id), cooldown_reason_value)
    last_sent_at = cooldowns.get(cooldown_key)
    cooldown_sec = max(int(config.ADMIN_NOTIFY_COOLDOWN_SEC), int(cooldown_min_sec or 0))
    if isinstance(last_sent_at, (int, float)) and (now - float(last_sent_at)) < cooldown_sec:
        return False

    admin_chat_ids = _admin_chat_ids_from_response(response)
    if not admin_chat_ids:
        logger.error(
            "ADMIN_NOTIFY_CHAT_IDS_EMPTY shift_id=%s reason=%s decisions=%s debug=%s",
            shift_id,
            reason,
            decisions,
            response.get("debug") if isinstance(response, dict) else None,
        )
        return False

    text_to_send = text or (
        "🚨 Нарушение по смене (server decision)\n"
        f"shift_id: {shift_id}\n"
        f"staff: {staff_name or '—'}\n"
        f"point_id: {point_id if point_id is not None else '—'}\n"
        f"last_seen: {_format_last_seen(last_ping_ts or 0)}\n"
        f"reason: {reason}\n"
        f"round: {round_value if round_value is not None else '—'}"
    )

    for admin_chat_id in admin_chat_ids:
        await context.bot.send_message(chat_id=admin_chat_id, text=text_to_send)

    cooldowns[cooldown_key] = now
    logger.info(
        "ADMIN_NOTIFY_SENT shift_id=%s reason=%s cooldown_reason=%s round=%s chats=%s",
        shift_id,
        reason,
        cooldown_reason_value,
        round_value,
        admin_chat_ids,
    )
    return True
