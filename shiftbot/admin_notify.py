import asyncio
import logging
from datetime import datetime, timezone

from shiftbot import config

logger = logging.getLogger(__name__)


async def notify_admin_hardcoded(context, session, reason: str, extra: dict | None = None) -> bool:
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
