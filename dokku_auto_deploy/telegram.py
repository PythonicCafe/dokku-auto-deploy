"""Minimal Telegram Bot API client (stdlib only): sending a message to a group or to a topic of a group."""

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API = "https://api.telegram.org"
# Answers usually take <1s, but some sendMessage calls took ~10s (seen 2026-09 with an invalid chat id; cause not
# found: not DNS, not a dead address). One call per deploy, so a generous timeout costs nothing.
HTTP_TIMEOUT = 30


class TelegramError(RuntimeError):
    pass


def parse_chat(chat: str) -> tuple[str, str | None]:
    """`-100123_45` -> ("-100123", "45") for a topic in a group; `-100123` -> ("-100123", None). A channel username
    (`@my_channel`) can have `_` too and has no topics."""
    match = re.fullmatch(r"(-?\d+)_(\d+)", chat)
    if match:
        return match[1], match[2]
    return chat, None


class Telegram:
    """The bot token is part of every request URL, so neither URLs nor this object's repr are ever shown."""

    def __init__(self, token: str, api: str = DEFAULT_API) -> None:
        self.token = token
        self.api = api.rstrip("/")

    def __repr__(self) -> str:
        return f"Telegram(api={self.api!r})"

    def send_message(self, chat: str, text: str, parse_mode: str = "HTML") -> None:
        """Send `text` to `chat` (see `parse_chat`); errors raise `TelegramError` without the token."""
        chat_id, thread_id = parse_chat(chat)
        fields = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if thread_id:
            fields["message_thread_id"] = thread_id
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(f"{self.api}/bot{self.token}/sendMessage", data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT):
                pass
        except urllib.error.HTTPError as exc:
            try:
                description = json.load(exc).get("description", "")
            except ValueError:
                description = ""
            raise TelegramError(f"Telegram API returned HTTP {exc.code}: {description or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"could not reach Telegram API: {exc.reason}") from None
        except TimeoutError:
            raise TelegramError(f"Telegram API did not answer in {HTTP_TIMEOUT}s") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # Only the type: messages of these (e.g. InvalidURL for a token with a newline) can contain the URL
            raise TelegramError(f"could not send the message ({type(exc).__name__})") from None
