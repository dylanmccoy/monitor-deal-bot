"""Webhook payloads and confirmed delivery, without exposing tokens."""

import html
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from config import webhook_id
from http_utils import RemoteError, retry_after, seconds


def safe_url(value: str) -> str | None:
    try:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        parts = urlsplit(html.unescape(value))
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or re.search(r"[\s()<>\[\]{}\\\"'`]", parts.netloc)
            or parts.port == 0
        ):
            return None
        # Percent-encode delimiters that could escape a Markdown link.
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                quote(parts.path, safe="/%:@!$&'*+,;=-._~"),
                quote(parts.query, safe="=&%/:?@!$'*+,;~-._"),
                quote(parts.fragment, safe="%~-._"),
            )
        )
    except ValueError:
        return None


def clean_text(text: str, limit: int) -> str:
    text = html.unescape(text)
    text = " ".join("".join(c for c in text if c >= " " and c != "\x7f").split())
    text = text.replace("@", "@\u200b")
    text = re.sub(r"([\\`*_{}\[\]()<>|~])", r"\\\1", text)
    # Discord counts UTF-16 code units. Avoid splitting surrogate pairs.
    return (
        text.encode("utf-16-le", errors="ignore")[: limit * 2].decode("utf-16-le", errors="ignore")
        or "Monitor deal"
    )


def build_payload(post, subreddit):
    reddit_url = safe_url(post.reddit_url) or "https://www.reddit.com/r/" + subreddit
    embed = {
        "title": clean_text(post.title, 256),
        "url": reddit_url,
        "color": 0x5865F2,
        "timestamp": datetime.fromtimestamp(post.created, timezone.utc).isoformat(),
        "footer": {"text": f"r/{subreddit}"},
        "fields": [
            {"name": "Discussion", "value": f"[Reddit thread]({reddit_url})", "inline": True}
        ],
    }
    link = safe_url(post.url)
    if link and len(link) <= 950 and link != reddit_url:
        embed["fields"].append(
            {"name": "Deal", "value": f"[Open deal page]({link})", "inline": True}
        )
    thumbnail = safe_url(post.thumbnail)
    if thumbnail and len(thumbnail) <= 2048:
        embed["thumbnail"] = {"url": thumbnail}
    return {"content": "New monitor deal", "embeds": [embed], "allowed_mentions": {"parse": []}}


class DiscordWebhook:
    def __init__(self, url, session=None, clock=time.time):
        webhook_id(url)
        self.url = url
        self.session = session or requests.Session()
        self.clock = clock
        self.not_before = 0

    def _request(self, method, suffix="", **kwargs):
        if self.clock() < self.not_before:
            raise RemoteError(
                "Discord", "waiting for rate limit reset", delay=self.not_before - self.clock()
            )
        try:
            response = self.session.request(
                method, self.url + suffix, timeout=(5, 15), allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            raise RemoteError("Discord", "connection failed; delivery may be unconfirmed") from None
        if response.headers.get("X-RateLimit-Remaining") == "0":
            self.not_before = max(
                self.not_before,
                self.clock() + seconds(response.headers.get("X-RateLimit-Reset-After")),
            )
        if response.status_code == 429:
            delay = max(1, retry_after(response))
            self.not_before = max(self.not_before, self.clock() + delay)
            raise RemoteError("Discord", "rate limited", status=429, delay=delay)
        return response

    @staticmethod
    def _error(response):
        raise RemoteError(
            "Discord",
            "request rejected" if response.status_code < 500 else "server error",
            status=response.status_code,
            permanent=400 <= response.status_code < 500,
        )

    def send(self, payload):
        response = self._request("POST", params={"wait": "true"}, json=payload)
        if response.status_code != 200:
            self._error(response)
        try:
            body = response.json()
        except ValueError:
            body = None
        message_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(message_id, str) or not message_id.isdigit():
            raise RemoteError(
                "Discord", "message acknowledgement missing; delivery may be unconfirmed"
            )
        return message_id

    def delete(self, message_id):
        if not isinstance(message_id, str) or not message_id.isdigit():
            raise ValueError("Invalid stored Discord message ID")
        response = self._request("DELETE", f"/messages/{message_id}")
        if response.status_code == 204:
            return
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = None
            # Unknown message is idempotent success. Unknown webhook is an
            # access/configuration failure and must retain the cleanup record.
            if isinstance(body, dict) and body.get("code") == 10008:
                return
        self._error(response)

    def close(self):
        self.session.close()
