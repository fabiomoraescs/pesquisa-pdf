"""Histórico efêmero, limitado e isolado por usuário/projeto/Base do Assistente."""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from threading import RLock
from typing import Any

from flask import session


MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_MESSAGE_CHARS = 1_200
CONVERSATION_TTL_SECONDS = 60 * 45


class AssistantConversationStore:
    """Armazena somente texto recente no processo; não usa banco nem provider."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], tuple[float, list[dict[str, str]]]] = {}
        self._lock = RLock()

    @staticmethod
    def _trim(text: object) -> str:
        return " ".join(str(text).split())[:MAX_HISTORY_MESSAGE_CHARS]

    def _session_token(self) -> str:
        token = session.get("assistant_conversation_token")
        if not isinstance(token, str) or len(token) < 20:
            token = secrets.token_urlsafe(24)
            session["assistant_conversation_token"] = token
        return token

    def _key(self, user_id: object, scope: str) -> tuple[str, str, str]:
        return str(user_id), self._session_token(), scope

    def read(self, user_id: object, scope: str) -> list[dict[str, str]]:
        now = time.monotonic()
        key = self._key(user_id, scope)
        with self._lock:
            saved = self._entries.get(key)
            if saved is None or now - saved[0] > CONVERSATION_TTL_SECONDS:
                self._entries.pop(key, None)
                return []
            return [dict(item) for item in saved[1]]

    def append(self, user_id: object, scope: str, question: str, answer: str) -> None:
        key = self._key(user_id, scope)
        entries = [
            {"role": "user", "content": self._trim(question)},
            {"role": "assistant", "content": self._trim(answer)},
        ]
        with self._lock:
            previous = self._entries.get(key, (0, []))[1]
            self._entries[key] = (time.monotonic(), [*previous, *entries][-MAX_HISTORY_MESSAGES:])

    def clear_scope(self, user_id: object, scope: str) -> None:
        with self._lock:
            self._entries.pop(self._key(user_id, scope), None)


conversation_store = AssistantConversationStore()


def conversation_scope(context_key: str, project_context: Mapping[str, Any] | None) -> str:
    """Nunca mistura mensagens de projetos ou Bases diferentes na mesma chamada."""
    project = project_context.get("project", {}) if isinstance(project_context, Mapping) else {}
    analysis = project_context.get("analysis", {}) if isinstance(project_context, Mapping) else {}
    selected = analysis.get("selected", {}) if isinstance(analysis, Mapping) else {}
    return "|".join((context_key, str(project.get("id") or "none"), str(selected.get("id") or "none")))


__all__ = [
    "CONVERSATION_TTL_SECONDS", "MAX_HISTORY_MESSAGES", "MAX_HISTORY_MESSAGE_CHARS",
    "AssistantConversationStore", "conversation_scope", "conversation_store",
]
