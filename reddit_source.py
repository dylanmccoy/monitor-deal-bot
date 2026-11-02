"""Approved, read-only OAuth access to Reddit listings."""

import re
import time

import requests

from http_utils import RemoteError, retry_after, seconds
from models import Post

TIMEOUT = (5, 15)


def parse_listing(body) -> tuple[list[Post], str | None]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise RemoteError("Reddit", "invalid listing response")
    data = body["data"]
    if not isinstance(data.get("children"), list):
        raise RemoteError("Reddit", "missing listing children")
    after = data.get("after")
    if after is not None and (
        not isinstance(after, str) or not re.fullmatch(r"t3_[a-z0-9]+", after)
    ):
        raise RemoteError("Reddit", "invalid pagination cursor")
    posts = []
    for child in data["children"]:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            raise RemoteError("Reddit", "invalid listing child")
        try:
            posts.append(Post.from_reddit(child.get("data")))
        except ValueError:
            raise RemoteError("Reddit", "invalid post fields") from None
    return posts, after


class RedditSource:
    def __init__(self, config, session=None, clock=time.time):
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock
        self.token = ""
        self.token_expires = 0
        self.not_before = 0

    def _gate(self):
        if self.clock() < self.not_before:
            raise RemoteError(
                "Reddit", "waiting for rate limit reset", delay=self.not_before - self.clock()
            )

    def _check(self, response):
        if response.headers.get("X-Ratelimit-Remaining") is not None:
            if seconds(response.headers["X-Ratelimit-Remaining"]) < 1:
                self.not_before = max(
                    self.not_before,
                    self.clock() + seconds(response.headers.get("X-Ratelimit-Reset")),
                )
        status = response.status_code
        if status == 429:
            delay = max(1, retry_after(response))
            self.not_before = max(self.not_before, self.clock() + delay)
            raise RemoteError("Reddit", "rate limited", status=status, delay=delay)
        if status != 200:
            permanent = status in (400, 401, 403, 404)
            raise RemoteError(
                "Reddit",
                "request rejected" if permanent else "request failed",
                status=status,
                permanent=permanent,
            )
        try:
            return response.json()
        except ValueError:
            raise RemoteError("Reddit", "non-JSON response") from None

    def _authorize(self):
        self._gate()
        data = {"grant_type": "client_credentials"}
        if self.config.refresh_token:
            data = {"grant_type": "refresh_token", "refresh_token": self.config.refresh_token}
        try:
            response = self.session.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(self.config.client_id, self.config.client_secret),
                data=data,
                headers={"User-Agent": self.config.user_agent},
                timeout=TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RemoteError("Reddit", "OAuth connection failed") from None
        body = self._check(response)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("access_token"), str)
            or not body["access_token"]
            or seconds(body.get("expires_in")) <= 0
        ):
            raise RemoteError("Reddit", "OAuth token missing or invalid", permanent=True)
        self.token = body["access_token"]
        self.token_expires = self.clock() + max(1, seconds(body["expires_in"]) - 30)

    def _get(self, endpoint, params):
        self._gate()
        for attempt in range(2):
            if not self.token or self.clock() >= self.token_expires:
                self._authorize()
            self._gate()
            try:
                response = self.session.get(
                    "https://oauth.reddit.com" + endpoint,
                    params=params,
                    headers={
                        "User-Agent": self.config.user_agent,
                        "Authorization": f"Bearer {self.token}",
                    },
                    timeout=TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise RemoteError("Reddit", "connection failed") from None
            if response.status_code == 401 and attempt == 0:
                self.token = ""
                continue
            return self._check(response)
        raise RemoteError("Reddit", "authentication failed", permanent=True)

    def listing(self, after=None):
        params = {"limit": 100, "raw_json": 1}
        if after:
            params["after"] = after
        return parse_listing(self._get(f"/r/{self.config.subreddit}/new", params))

    def lookup(self, ids):
        if not ids or len(ids) > 100:
            raise ValueError("Lookup requires 1 to 100 post IDs")
        posts, _ = parse_listing(self._get("/api/info", {"id": ",".join(ids), "raw_json": 1}))
        return {post.id: post for post in posts}

    def close(self):
        self.session.close()
