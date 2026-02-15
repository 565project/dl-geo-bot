from typing import Dict

from shiftbot.models import ShiftSession


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[int, ShiftSession] = {}

    def get_or_create(self, user_id: int, chat_id: int) -> ShiftSession:
        session = self._sessions.get(user_id)
        if not session:
            session = ShiftSession(user_id=user_id, chat_id=chat_id)
            self._sessions[user_id] = session
        else:
            session.chat_id = chat_id
        return session

    def values(self):
        return self._sessions.values()

    def is_empty(self) -> bool:
        return not self._sessions
