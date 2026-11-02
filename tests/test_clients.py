from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests
from conftest import changed, reddit_post, response

from discord_webhook import DiscordWebhook, build_payload, safe_url
from http_utils import RemoteError
from reddit_source import RedditSource, parse_listing


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"data": {}},
        {"data": {"children": None}},
        {"data": {"children": [{"kind": "t3", "data": {}}]}},
    ],
)
def test_invalid_listing_is_failure(body):
    with pytest.raises(RemoteError):
        parse_listing(body)


def test_normalization_drops_author_and_body(post):
    child = reddit_post(post)
    child["data"].update(author="private", selftext="private", created_utc=True)
    with pytest.raises(RemoteError):
        parse_listing({"data": {"children": [child]}})
    child["data"]["created_utc"] = post.created
    parsed, after = parse_listing({"data": {"children": [child], "after": None}})
    assert parsed[0] == post
    assert "private" not in str(parsed[0].to_dict())


def test_oauth_refresh_once_and_authorization_only_to_reddit(config, clock, post):
    session = Mock()
    session.post.side_effect = [
        response(body={"access_token": "one", "expires_in": 3600}),
        response(body={"access_token": "two", "expires_in": 3600}),
    ]
    session.get.side_effect = [
        response(401),
        response(body={"data": {"children": [reddit_post(post)], "after": None}}),
    ]
    source = RedditSource(config, session, clock)
    posts, _ = source.listing()
    assert posts == [post]
    assert session.post.call_count == 2
    assert session.get.call_count == 2
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer two"
    assert session.post.call_args.kwargs["data"] == {"grant_type": "client_credentials"}
    assert session.post.call_args.kwargs["allow_redirects"] is False


def test_refresh_token_and_expiry(config, clock):
    session = Mock()
    session.post.return_value = response(body={"access_token": "test", "expires_in": 3600})
    session.get.return_value = response(body={"data": {"children": [], "after": None}})
    source = RedditSource(replace(config, refresh_token="refresh-secret"), session, clock)
    source.listing()
    assert session.post.call_args.kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-secret",
    }
    clock.advance(3600)
    source.listing()
    assert session.post.call_count == 2


def test_reddit_rate_limit_header_prevents_next_request(config, clock):
    session = Mock()
    session.post.return_value = response(body={"access_token": "test", "expires_in": 3600})
    session.get.return_value = response(
        body={"data": {"children": []}},
        headers={"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "123"},
    )
    source = RedditSource(config, session, clock)
    source.listing()
    with pytest.raises(RemoteError) as caught:
        source.listing()
    assert caught.value.delay == 123
    assert session.get.call_count == 1


def test_persistent_401_only_refreshes_once(config, clock):
    session = Mock()
    session.post.return_value = response(body={"access_token": "test", "expires_in": 3600})
    session.get.return_value = response(401)
    with pytest.raises(RemoteError) as caught:
        RedditSource(config, session, clock).listing()
    assert caught.value.permanent
    assert session.get.call_count == 2


def test_discord_ack_and_rate_limit(config, clock):
    session = Mock()
    session.request.return_value = response(
        body={"id": "987"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.5"}
    )
    client = DiscordWebhook(config.webhook_url, session, clock)
    assert client.send({"content": "test"}) == "987"
    assert session.request.call_args.kwargs["params"] == {"wait": "true"}
    with pytest.raises(RemoteError) as caught:
        client.send({"content": "test"})
    assert caught.value.delay == 2.5
    assert session.request.call_count == 1


def test_429_is_seconds_not_milliseconds(config, clock):
    session = Mock()
    session.request.return_value = response(429, {"retry_after": 65.5}, {"Retry-After": "60"})
    client = DiscordWebhook(config.webhook_url, session, clock)
    with pytest.raises(RemoteError) as caught:
        client.send({"content": "test"})
    assert caught.value.delay == 65.5
    assert client.not_before == clock() + 65.5


@pytest.mark.parametrize("body", [None, {}, {"id": 123}, {"id": "oops"}])
def test_missing_ack_never_counts_as_success(config, clock, body):
    session = Mock()
    session.request.return_value = response(body=body)
    with pytest.raises(RemoteError, match="acknowledgement"):
        DiscordWebhook(config.webhook_url, session, clock).send({"content": "test"})


def test_secrets_never_appear_in_safe_errors(config, clock):
    session = Mock()
    session.request.side_effect = requests.ConnectionError(config.webhook_url)
    with pytest.raises(RemoteError) as caught:
        DiscordWebhook(config.webhook_url, session, clock).send({"content": "test"})
    assert "test_token" not in str(caught.value)
    assert config.webhook_url not in str(caught.value)


def test_payload_escapes_mentions_markdown_and_limits(post):
    post = changed(
        post,
        title="@everyone [link](https://evil.example) " + "🖥" * 300,
        url="https://shop.example/a)b?x=1) @everyone",
        thumbnail="self",
    )
    payload = build_payload(post, "buildapcsales")
    assert payload["allowed_mentions"] == {"parse": []}
    embed = payload["embeds"][0]
    assert len(embed["title"].encode("utf-16-le")) <= 512
    assert "@everyone" not in embed["title"]
    assert "thumbnail" not in embed
    assert ") @everyone" not in embed["fields"][1]["value"]
    assert "%29" in embed["fields"][1]["value"]


def test_invalid_deal_url_is_omitted(post):
    payload = build_payload(changed(post, url="javascript:alert(1)"), "buildapcsales")
    assert len(payload["embeds"][0]["fields"]) == 1


@pytest.mark.parametrize("code,success", [(10008, True), (10015, False), (0, False)])
def test_cleanup_404_distinguishes_message_from_webhook(config, clock, code, success):
    session = Mock()
    session.request.return_value = response(404, {"code": code})
    client = DiscordWebhook(config.webhook_url, session, clock)
    if success:
        client.delete("987")
    else:
        with pytest.raises(RemoteError) as caught:
            client.delete("987")
        assert caught.value.permanent


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example)@everyone/x",
        "https://shop.example)/x",
        "https://shop.example:bad/x",
        "https://shop.example/\nfoo",
    ],
)
def test_unsafe_url_authority_or_controls_are_omitted(url):
    assert safe_url(url) is None
