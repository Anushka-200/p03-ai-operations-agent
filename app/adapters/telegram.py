"""Telegram Bot API over plain HTTPS (httpx): long polling, so no public URL or tunnel is needed.

Analogy: we call the post office every few seconds and ask "anything for me?" instead of giving the
post office our address. The bot token is a password: it sits in the URL path, so no error message
or log line may ever contain the URL (httpx errors do, which is why we re-raise a clean one).
"""

import logging
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)
Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)


class TelegramError(RuntimeError):
    """Any failure talking to Telegram. The message never contains the token."""


class TelegramPort(Protocol):
    sends_for_real: bool

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]: ...
    def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int | None: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def edit_message(self, chat_id: int, message_id: int, text: str) -> None: ...


class TelegramApi:
    def __init__(self, token: str, send_enabled: bool, client: httpx.Client | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self.sends_for_real = send_enabled
        self._client = client or httpx.Client()

    def _call(self, method: str, payload: dict[str, Any], timeout: float = 15.0) -> Any:
        try:
            resp = self._client.post(f"{self._base}/{method}", json=payload, timeout=timeout)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from None  # no URL, no token
        if not data.get("ok"):
            raise TelegramError(f"{method} refused: {str(data.get('description', 'unknown'))[:120]}")
        return data["result"]

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        # Reading is always allowed (it changes nothing). Server waits up to `timeout` s for news.
        return self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 10,
        )

    def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int | None:
        if not self.sends_for_real:
            logger.info("telegram dry-run: would send", extra={"chars": len(text), "buttons": bool(buttons)})
            return None
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}  # plain text: no markup injection
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]
            }
        return self._call("sendMessage", payload)["message_id"]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        if self.sends_for_real:  # clears the spinner on the owner's button
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        if self.sends_for_real:  # replaces the buttons with the outcome so they cannot be tapped twice
            self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]})


class FakeTelegram:
    """In-memory Telegram for tests: feed it updates, read what the bot sent."""

    sends_for_real = True

    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self._id = 100

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        out = [u for u in self.queue if u["update_id"] >= offset]
        return out

    def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int | None:
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": self._id})
        return self._id

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(text)

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
