import json
import sqlite3

import pytest
from conftest import changed

from http_utils import RemoteError
from state import State, worker_lock
from worker import RETENTION, Worker


def setup(config, services, clock):
    state = State(config.db_path)
    worker = Worker(config, state, *services, clock=clock)
    assert worker.tick()
    clock.advance(61)
    return state, worker


def discover_post(services, post):
    source, _ = services
    source.pages[None] = ([post], None)
    source.current[post.id] = post


def test_initial_seed_new_delivery_and_restart(config, services, clock, post):
    source, discord = services
    old = changed(post, id="t3_old", created=clock() - 60)
    source.pages[None] = ([old], None)
    state, worker = setup(config, services, clock)
    assert not discord.payloads
    assert state.summary() == {"suppressed": 1}
    source.pages[None] = ([post, old], None)
    assert worker.tick()
    assert len(discord.payloads) == 1
    assert state.summary() == {"sent": 1, "suppressed": 1}
    assert (
        state.db.execute("SELECT payload FROM posts WHERE id=?", (post.id,)).fetchone()[0] is None
    )
    state.close()

    with_state = State(config.db_path)
    resumed = Worker(config, with_state, source, discord, clock)
    assert resumed.tick()
    assert len(discord.payloads) == 1
    with_state.close()


def test_failed_initialization_cannot_flood_history(config, services, clock, post):
    source, discord = services
    source.failure = RemoteError("Reddit", "connection failed")
    state = State(config.db_path)
    worker = Worker(config, state, source, discord, clock)
    assert not worker.tick()
    assert state.get("initialized") is None
    source.failure = None
    clock.advance(1000)
    discover_post(services, post)
    assert worker.tick()
    assert not discord.payloads
    assert state.summary() == {"suppressed": 1}
    state.close()


@pytest.mark.parametrize("status", [429, 503, None])
def test_delivery_retry_is_durable_and_rechecks_source(config, services, clock, post, status):
    state, worker = setup(config, services, clock)
    source, discord = services
    discover_post(services, post)
    discord.failure = RemoteError("Discord", "failed", status=status, delay=60)
    assert not worker.tick()
    row = state.pending(clock() + 1000)[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert json.loads(row["payload"])["id"] == post.id
    state.close()

    clock.advance(1000)
    discord.failure = None
    state = State(config.db_path)
    resumed = Worker(config, state, source, discord, clock)
    assert resumed.tick()
    assert state.summary() == {"sent": 1}
    assert len(discord.payloads) == 1
    state.close()


def test_retry_of_removed_post_is_expired(config, services, clock, post):
    state, worker = setup(config, services, clock)
    _, discord = services
    discover_post(services, post)
    discord.failure = RemoteError("Discord", "timeout")
    worker.tick()
    discord.failure = None
    services[0].current[post.id] = changed(post, removed=True)
    clock.advance(61)
    assert worker.tick()
    assert state.summary() == {"expired": 1}
    assert not discord.payloads
    state.close()


def test_source_outage_does_not_block_already_queued_first_delivery(config, services, clock, post):
    state, worker = setup(config, services, clock)
    with state.db:
        state.observe(post, "pending", "monitor")
    services[0].failure = RemoteError("Reddit", "down")
    assert not worker.tick()
    assert len(services[1].payloads) == 1
    state.close()


def test_discord_outage_does_not_block_discovery(config, services, clock, post):
    state, worker = setup(config, services, clock)
    discover_post(services, post)
    services[1].failure = RemoteError("Discord", "forbidden", status=403, permanent=True)
    assert not worker.tick()
    assert worker.discord_paused
    clock.advance(61)
    other = changed(post, id="t3_other", created=clock() - 1)
    services[0].pages[None] = ([other, post], None)
    worker.tick()
    assert state.summary() == {"pending": 2}
    clock.advance(config.max_age + 1)
    worker.tick()
    assert state.summary() == {"expired": 2}
    assert all(row[0] is None for row in state.db.execute("SELECT payload FROM posts"))
    state.close()


def test_invalid_payload_does_not_block_next_post(config, services, clock, post, monkeypatch):
    state, worker = setup(config, services, clock)
    discover_post(services, post)
    other = changed(post, id="t3_other", created=post.created + 1)
    services[0].pages[None] = ([other, post], None)
    send = services[1].send

    def maybe_send(payload):
        if not getattr(maybe_send, "called", False):
            maybe_send.called = True
            raise RemoteError("Discord", "invalid", status=400, permanent=True)
        return send(payload)

    monkeypatch.setattr(services[1], "send", maybe_send)
    assert not worker.tick()
    assert state.summary() == {"failed": 1, "sent": 1}
    state.close()


def test_two_pages_and_deduplication(config, services, clock, post):
    state, worker = setup(config, services, clock)
    source, discord = services
    # More posts than one Discord pass; each listing page has 100 items.
    first = [changed(post, id=f"t3_a{i}") for i in range(100)]
    second = [changed(post, id=f"t3_b{i}") for i in range(100)]
    source.pages = {None: (first, "t3_cursor"), "t3_cursor": (second, None)}
    assert worker.tick()
    assert source.calls[-2:] == [None, "t3_cursor"]
    assert sum(state.summary().values()) == 200
    assert state.get("scan") is None
    assert state.get("checkpoint") == clock()
    for _ in range(9):
        worker.tick()
    assert len(discord.payloads) == 200
    assert state.summary() == {"sent": 200}
    state.close()


def test_page_budget_resumes_after_restart(config, services, clock, post):
    state, worker = setup(config, services, clock)
    source, _ = services
    initial_checkpoint = state.get("checkpoint")
    source.pages = {None: ([post], "t3_c1")}
    for i in range(1, 12):
        source.pages[f"t3_c{i}"] = (
            [changed(post, id=f"t3_page{i}")],
            f"t3_c{i + 1}" if i < 11 else None,
        )
    assert not worker.tick()
    assert state.get("checkpoint") == initial_checkpoint
    assert state.get("scan")["cursor"] == "t3_c10"
    original_target = clock()
    state.close()
    clock.advance(61)
    state = State(config.db_path)
    resumed = Worker(config, state, *services, clock)
    assert resumed.tick()
    assert state.get("scan") is None
    assert state.get("checkpoint") == original_target
    assert source.calls[-3:] == [None, "t3_c10", "t3_c11"]
    assert sum(state.summary().values()) == 12
    state.close()


def test_partial_fetch_failure_does_not_advance_checkpoint(config, services, clock, post):
    state, worker = setup(config, services, clock)
    before = state.get("checkpoint")
    source, _ = services
    source.pages[None] = ([post], "t3_cursor")

    def listing(after=None):
        if after:
            raise RemoteError("Reddit", "invalid listing response")
        return [post], "t3_cursor"

    source.listing = listing
    assert not worker.tick()
    assert state.get("checkpoint") == before
    assert state.summary() == {}
    state.close()


@pytest.mark.parametrize("deleted", [True, False])
def test_source_deletion_and_retention_cleanup(config, services, clock, post, deleted):
    state, worker = setup(config, services, clock)
    discover_post(services, post)
    assert worker.tick()
    clock.advance(901 if deleted else RETENTION + 1)
    if deleted:
        services[0].current[post.id] = changed(post, removed=True)
    else:
        services[0].failure = RemoteError("Reddit", "offline")
    worker.tick()
    assert services[1].deleted == ["101"]
    assert state.db.execute("SELECT message_id FROM posts").fetchone()[0] is None
    state.close()


def test_cleanup_failures_resume_on_restart(config, services, clock, post):
    state, worker = setup(config, services, clock)
    discover_post(services, post)
    worker.tick()
    clock.advance(RETENTION + 1)
    services[1].failure = RemoteError("Discord", "server error", status=503)
    worker.tick()
    assert state.db.execute("SELECT cleanup FROM posts").fetchone()[0] == 1
    state.close()
    clock.advance(1000)
    services[1].failure = None
    state = State(config.db_path)
    worker = Worker(config, state, *services, clock)
    worker.tick()
    assert services[1].deleted == ["101"]
    state.close()


def test_different_destination_and_second_worker_rejected(config):
    with worker_lock(config.db_path):
        with pytest.raises(ValueError, match="Another worker"):
            with worker_lock(config.db_path):
                pass
        state = State(config.db_path)
        state.bind("buildapcsales", "123")
        with pytest.raises(ValueError, match="different subreddit or webhook"):
            state.bind("buildapcsales", "456")
        state.close()


def test_ambiguous_send_can_duplicate_but_is_not_lost(config, services, clock, post):
    state, worker = setup(config, services, clock)
    discover_post(services, post)
    original = services[1].send

    def ambiguous(payload):
        original(payload)  # Remote side accepts, then response is lost.
        raise RemoteError("Discord", "connection failed")

    services[1].send = ambiguous
    worker.tick()
    services[1].send = original
    clock.advance(61)
    worker.tick()
    assert len(services[1].payloads) == 2
    assert state.summary() == {"sent": 1}
    state.close()


def test_many_retries_cannot_starve_first_attempts_during_source_outage(
    config, services, clock, post
):
    state, worker = setup(config, services, clock)
    with state.db:
        for index in range(25):
            old = changed(post, id=f"t3_retry{index}", created=post.created - 1)
            state.observe(old, "pending", "monitor")
            state.retry(old.id, 0, "safe error")
        state.observe(post, "pending", "monitor")
    services[0].failure = RemoteError("Reddit", "down")
    assert not worker.tick()
    assert len(services[1].payloads) == 1
    assert state.summary() == {"pending": 25, "sent": 1}
    state.close()


def test_ack_commit_failure_leaves_recoverable_pending_state(
    config, services, clock, post, monkeypatch
):
    state, worker = setup(config, services, clock)
    discover_post(services, post)

    def cannot_commit(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(state, "sent", cannot_commit)
    with pytest.raises(sqlite3.OperationalError):
        worker.tick()
    assert len(services[1].payloads) == 1
    assert state.summary() == {"pending": 1}
    state.close()
