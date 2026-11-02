import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import changed, reddit_post

from config import ROOT, Config, webhook_id
from filters import classify
from monitor_deals import main
from state import State


@pytest.mark.parametrize(
    "title,flair,expected",
    [
        ("[Monitor] IPS 1080p 75Hz $99", "Monitor", True),
        ("Example OLED 4K", "Monitors", True),
        ("[Monitor] Example 240Hz", "", True),
        ("[Monitors] Example display", "Changed flair", True),
        ("[Monitor Arm] Desk mount", "Monitor Arm", False),
        ("[Monitor] Dual monitor stand", "Monitor", False),
        ("[Monitor] Display $199", "Expired", False),
        ("[Monitor] Display - SOLD OUT", "Monitor", False),
        ("monitor software", "Software", False),
        ("[Discussion] Which monitor?", "Discussion", False),
    ],
)
def test_classification(config, post, title, flair, expected):
    assert classify(changed(post, title=title, flair=flair), config)[0] is expected


def test_keywords_and_exclusion_priority(config, post):
    post = changed(post, title="[Monitor] OLED mini LED 240 Hz 1440p")
    assert classify(post, config)[0]
    assert classify(post, replace(config, include=("240Hz", "mini-led"), keyword_mode="all"))[0]
    assert not classify(post, replace(config, include=("4K", "OLED"), keyword_mode="all"))[0]
    assert classify(post, replace(config, include=("4K", "OLED"), keyword_mode="any"))[0]
    assert not classify(post, replace(config, include=("oled",), exclude=("oled",)))[0]
    assert not classify(
        changed(post, title="[Monitor] 1240Hz"), replace(config, include=("240Hz",))
    )[0]


def test_offline_dry_run_does_not_create_or_change_db(config, post, tmp_path, monkeypatch, capsys):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"data": {"children": [reddit_post(post)]}}))
    monkeypatch.setattr(
        Config, "load", lambda: replace(config, client_id="", client_secret="", webhook_url="")
    )
    assert main(["--dry-run", "--once", "--fixture", str(fixture)]) == 0
    assert not config.db_path.exists()
    result = json.loads(capsys.readouterr().out)
    assert result["match"] is True
    config.db_path.write_bytes(b"production sentinel")
    assert main(["--dry-run", "--fixture", str(fixture)]) == 0
    assert config.db_path.read_bytes() == b"production sentinel"


def test_webhook_test_requires_no_reddit_or_state(config, monkeypatch):
    monkeypatch.setattr(Config, "load", lambda: replace(config, client_id="", client_secret=""))
    client = Mock()
    client.send.return_value = "123"
    monkeypatch.setattr("monitor_deals.DiscordWebhook", lambda url: client)
    assert main(["--test-webhook"]) == 0
    assert "synthetic test" in client.send.call_args.args[0]["content"]
    assert not config.db_path.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/test",
        "https://evil.example/api/webhooks/123/test",
        "https://discord.com.evil.example/api/webhooks/123/test",
        "https://discord.com/api/webhooks/123/test?token=bad",
        "https://discord.com:bad/api/webhooks/123/test",
    ],
)
def test_invalid_webhook_rejected(url):
    with pytest.raises(ValueError):
        webhook_id(url)


def test_env_precedence_and_project_relative_db(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("INCLUDE_KEYWORDS=OLED\nPOLL_INTERVAL_SECONDS=90\n")
    monkeypatch.setenv("INCLUDE_KEYWORDS", "4k")
    monkeypatch.setenv("STATE_DB_PATH", "./data/test.sqlite3")
    monkeypatch.chdir(tmp_path)
    config = Config.load(env_file)
    assert config.include == ("4k",)
    assert config.poll_interval == 90
    assert config.db_path == ROOT / "data/test.sqlite3"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "oops"])
def test_invalid_poll_interval_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", value)
    with pytest.raises(ValueError, match="positive number"):
        Config.load(tmp_path / "missing")


def test_cli_once_initializes_quietly(config, services, monkeypatch):
    source, discord = services
    source.close = lambda: None
    discord.close = lambda: None
    monkeypatch.setattr(Config, "load", lambda: config)
    monkeypatch.setattr("monitor_deals.RedditSource", lambda config: source)
    monkeypatch.setattr("monitor_deals.DiscordWebhook", lambda url: discord)
    assert main(["--once"]) == 0
    assert not discord.payloads
    state = State(config.db_path)
    assert state.get("initialized") is not None
    state.close()


def test_cli_once_persisted_cooldown_is_not_a_success(config, services, monkeypatch):
    import time

    source, discord = services
    source.close = lambda: None
    discord.close = lambda: None
    monkeypatch.setattr(Config, "load", lambda: config)
    monkeypatch.setattr("monitor_deals.RedditSource", lambda config: source)
    monkeypatch.setattr("monitor_deals.DiscordWebhook", lambda url: discord)
    state = State(config.db_path)
    with state.db:
        state.set("reddit_not_before", time.time() + 3600)
    state.close()
    assert main(["--once"]) == 1
    assert not source.calls
