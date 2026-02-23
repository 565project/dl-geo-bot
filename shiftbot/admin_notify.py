import asyncio
import logging
import time
from datetime import datetime, timezone

from shiftbot import config

logger = logging.getLogger(__name__)

NOTIFY_ADMINS_COOLDOWN_KEY = "notify_admins_cooldowns"


async def notify_admins(
    context,
    message: str,
    shift_id=None,
    cooldown_key: str | None = None,
) -> bool:
    """Send message to all admin chat IDs fetched dynamically from the API.

    Respects cooldown if cooldown_key is provided.
    Logs every send attempt.
    Returns True if at least one message was sent successfully.
    """
    app = context.application if context else None
    if app is None:
        logger.warning("NOTIFY_ADMINS_NO_APP message=%s", message[:80])
        return False

    # Cooldown check
    if cooldown_key is not None:
        cooldowns = app.bot_data.setdefault(NOTIFY_ADMINS_COOLDOWN_KEY, {})
        now = time.time()
        ck = (str(shift_id) if shift_id is not None else "", cooldown_key)
        last_sent = cooldowns.get(ck)
        if isinstance(last_sent, float) and (now - last_sent) < config.ADMIN_NOTIFY_COOLDOWN_SEC:
            logger.debug(
                "NOTIFY_ADMINS_COOLDOWN shift_id=%s cooldown_key=%s remaining=%.0fs",
                shift_id,
                cooldown_key,
                config.ADMIN_NOTIFY_COOLDOWN_SEC - (now - last_sent),
            )
            return False

    # Resolve admin chat IDs via API (with cache + fallback)
    oc_client = app.bot_data.get("oc_client")
    if oc_client is not None:
        try:
            chat_ids = await oc_client.get_admin_chat_ids()
        except Exception as exc:
            logger.warning("NOTIFY_ADMINS_GET_IDS_FAILED error=%s, using fallback", exc)
            chat_ids = list(config.ADMIN_FORCE_CHAT_IDS)
    else:
        chat_ids = list(config.ADMIN_FORCE_CHAT_IDS)

    if not chat_ids:
        logger.warning("NOTIFY_ADMINS_NO_CHAT_IDS shift_id=%s message=%s", shift_id, message[:80])
        return False

    sent_any = False
    for chat_id in chat_ids:
        logger.info(
            "NOTIFY_ADMINS_ATTEMPT chat_id=%s shift_id=%s cooldown_key=%s",
            chat_id,
            shift_id,
            cooldown_key,
        )
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            logger.info("NOTIFY_ADMINS_OK chat_id=%s shift_id=%s", chat_id, shift_id)
            sent_any = True
        except Exception as exc:
            logger.error(
                "NOTIFY_ADMINS_ERROR chat_id=%s shift_id=%s error=%s",
                chat_id,
                shift_id,
                exc,
            )

    if sent_any and cooldown_key is not None:
        cooldowns = app.bot_data.setdefault(NOTIFY_ADMINS_COOLDOWN_KEY, {})
        ck = (str(shift_id) if shift_id is not None else "", cooldown_key)
        cooldowns[ck] = time.time()

    return sent_any


async def notify_admin_hardcoded(context, session, reason: str, extra: dict | None = None) -> bool:
    """Legacy hardcoded admin notifier. Disabled when HARD_ADMIN_ENABLED=False."""
    if not config.HARD_ADMIN_ENABLED:
        return False

    await asyncio.sleep(config.HARD_ADMIN_DELAY_SEC)

    shift_id = getattr(session, "shift_id", None) or getattr(session, "active_shift_id", None)
    staff_id = getattr(session, "staff_id", None) or getattr(session, "user_id", None)
    point_id = getattr(session, "point_id", None) or getattr(session, "active_point_id", None)
    user_chat_id = getattr(session, "chat_id", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    text = (
        "🚨 ADMIN TEST ALERT\n\n"
        f"reason={reason}\n"
        f"shift_id={shift_id}\n"
        f"staff_id={staff_id}\n"
        f"point_id={point_id}\n"
        f"user_chat_id={user_chat_id}\n"
        f"now={now_iso}"
    )
    if extra:
        text += f"\nextra={extra}"

    logger.info(
        "ADMIN_SEND_ATTEMPT chat_id=%s reason=%s shift_id=%s staff_id=%s point_id=%s user_chat_id=%s",
        config.HARD_ADMIN_CHAT_ID,
        reason,
        shift_id,
        staff_id,
        point_id,
        user_chat_id,
    )

    try:
        await context.bot.send_message(chat_id=config.HARD_ADMIN_CHAT_ID, text=text)
    except Exception as exc:
        logger.error(
            "ADMIN_SEND_ERROR chat_id=%s reason=%s shift_id=%s staff_id=%s error=%s",
            config.HARD_ADMIN_CHAT_ID,
            reason,
            shift_id,
            staff_id,
            exc,
            exc_info=True,
        )
        return False

    logger.info(
        "ADMIN_SEND_OK chat_id=%s reason=%s shift_id=%s staff_id=%s",
        config.HARD_ADMIN_CHAT_ID,
        reason,
        shift_id,
        staff_id,
    )
    return True
