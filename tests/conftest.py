from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from config import Config
from models import Post


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not use the network")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def config(tmp_path):
    return Config(
        webhook_url="https://discord.com/api/webhooks/123/test_token",
        client_id="test_client",
        client_secret="test_secret",
        user_agent="linux:test:v1 (by /u/testuser)",
        db_path=tmp_path / "deals.sqlite3",
    )


@pytest.fixture
def clock():
    class Clock:
        now = 1_800_000_000.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    return Clock()


@pytest.fixture
def post(clock):
    return Post(
        "t3_new",
        '[Monitor] Example 27" 1440p IPS 240 Hz - $199',
        "Monitor",
        clock() + 1,
        "https://www.reddit.com/r/buildapcsales/comments/new/example/",
        "https://example.com/monitor",
    )


def response(status=200, body=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = body
    return result


def reddit_post(post):
    return {
        "kind": "t3",
        "data": {
            "name": post.id,
            "title": post.title,
            "created_utc": post.created,
            "link_flair_text": post.flair,
            "permalink": post.reddit_url.removeprefix("https://www.reddit.com"),
            "url": post.url,
            "thumbnail": post.thumbnail,
            "removed_by_category": "deleted" if post.removed else None,
            "is_self": post.discussion,
        },
    }


class Source:
    def __init__(self):
        self.not_before = 0
        self.pages = {None: ([], None)}
        self.current = {}
        self.calls = []
        self.failure = None

    def listing(self, after=None):
        self.calls.append(after)
        if self.failure:
            raise self.failure
        return self.pages[after]

    def lookup(self, ids):
        if self.failure:
            raise self.failure
        return {key: value for key, value in self.current.items() if key in ids}


class Discord:
    def __init__(self):
        self.not_before = 0
        self.payloads = []
        self.deleted = []
        self.failure = None

    def send(self, payload):
        if self.failure:
            raise self.failure
        self.payloads.append(payload)
        return str(len(self.payloads) + 100)

    def delete(self, message_id):
        if self.failure:
            raise self.failure
        self.deleted.append(message_id)


@pytest.fixture
def services():
    return Source(), Discord()


def changed(post, **kwargs):
    return replace(post, **kwargs)
