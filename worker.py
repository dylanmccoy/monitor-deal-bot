"""Single-process scheduler with separate discovery and delivery due times."""

import json
import logging
import time

from discord_webhook import build_payload
from filters import classify, unavailable
from http_utils import RemoteError, backoff
from models import Post

LOG = logging.getLogger(__name__)
OVERLAP = 600
RETENTION = 86400
MAX_PAGES = 10


class Worker:
    def __init__(self, config, state, source, discord, clock=time.time):
        self.config, self.state = config, state
        self.source, self.discord = source, discord
        self.clock = clock
        self.poll_due = 0
        self.poll_attempts = 0
        self.discord_paused = False
        self.had_error = False
        self.source.not_before = max(self.source.not_before, state.get("reddit_not_before", 0))
        self.discord.not_before = max(self.discord.not_before, state.get("discord_not_before", 0))

    def _source_failure(self, error):
        self.had_error = True
        self.poll_attempts += 1
        delay = max(900 if error.permanent else 0, backoff(self.poll_attempts, error.delay))
        self.source.not_before = max(self.source.not_before, self.clock() + delay)
        self.poll_due = max(self.poll_due, self.source.not_before)
        LOG.error("%s; Reddit retry in %.1fs", error, delay)

    def discover(self):
        now = self.clock()
        if now < max(self.poll_due, self.source.not_before):
            return
        try:
            self._discover(now)
        except RemoteError as error:
            if error.status == 400 and self.state.get("scan"):
                with self.state.db:
                    self.state.set("scan", None)
                LOG.warning("Catch-up cursor rejected; restarting scan with coverage gap")
            self._source_failure(error)

    def _discover(self, started):
        posts, after = self.source.listing()
        boundary = self.state.get("initialized")
        if boundary is None:
            with self.state.db:
                for post in posts:
                    self.state.observe(post, "suppressed", "initial_history")
                self.state.set("initialized", started)
                self.state.set("checkpoint", started)
                self.state.set("last_poll", started)
            self.poll_due = self.clock() + self.config.poll_interval
            self.poll_attempts = 0
            LOG.info("Initialized quietly with %d existing posts", len(posts))
            return

        scan = self.state.get("scan")
        cutoff = max(
            boundary,
            started - self.config.max_age,
            self.state.get("checkpoint", boundary) - OVERLAP,
        )
        target = started
        cursor = after
        # Always inspect page one while an older scan is in progress.
        if scan:
            cutoff = max(scan["cutoff"], started - self.config.max_age, boundary)
            target = scan["target"]
            cursor = scan["cursor"]
        collected = list(posts)

        def reached(page):
            return bool(page) and min(p.created for p in page) <= cutoff

        complete = not cursor or (not scan and reached(posts))
        used = {cursor} if cursor else set()
        for _ in range(MAX_PAGES - 1):
            if complete:
                break
            page, next_cursor = self.source.listing(cursor)
            collected.extend(page)
            complete = reached(page) or next_cursor is None
            if not complete and (not page or next_cursor in used):
                raise RemoteError("Reddit", "pagination stopped making progress")
            cursor = next_cursor
            if cursor:
                used.add(cursor)

        accepted = 0
        with self.state.db:
            for post in sorted(
                {p.id: p for p in collected}.values(), key=lambda p: (p.created, p.id)
            ):
                matches, reason = classify(post, self.config)
                if post.created <= boundary:
                    status, reason = "suppressed", "initial_history"
                elif post.created <= started - self.config.max_age:
                    status, reason = "expired", "too_old"
                else:
                    status = "pending" if matches else "skipped"
                before = self.state.db.total_changes
                self.state.observe(post, status, reason)
                if status == "pending" and self.state.db.total_changes > before:
                    accepted += 1
            self.state.set("last_poll", started)
            if complete:
                self.state.set("checkpoint", target)
                self.state.set("scan", None)
            else:
                self.state.set("scan", {"cursor": cursor, "cutoff": cutoff, "target": target})
        if not complete:
            self.had_error = True
            LOG.warning("Discovery page budget reached; coverage gap, catch-up cursor saved")
        self.poll_attempts = 0
        self.poll_due = self.clock() + self.config.poll_interval
        LOG.info("Poll fetched=%d newly_queued=%d complete=%s", len(collected), accepted, complete)

    def deliver(self):
        now = self.clock()
        with self.state.db:
            self.state.expire(now, self.config.max_age)
        if self.discord_paused or now < self.discord.not_before:
            return
        for row in self.state.pending(now, include_retries=now >= self.source.not_before):
            try:
                post = Post(**json.loads(row["payload"]))
                if row["attempts"]:
                    # Verify freshness on retry; no API error may imply deletion.
                    if self.clock() < self.source.not_before:
                        continue
                    try:
                        post = self.source.lookup([row["id"]]).get(row["id"])
                    except RemoteError as error:
                        self._source_failure(error)
                        continue
                    if post is None or unavailable(post):
                        with self.state.db:
                            self.state.terminal(row["id"], "expired", "unavailable_on_retry")
                        continue
                message_id = self.discord.send(build_payload(post, self.config.subreddit))
            except RemoteError as error:
                self.had_error = True
                delay = backoff(row["attempts"] + 1, error.delay)
                with self.state.db:
                    if error.status == 400:
                        self.state.terminal(row["id"], "failed", "invalid_payload")
                    else:
                        self.state.retry(row["id"], self.clock() + delay, error)
                LOG.error("Delivery %s: %s", row["id"], error)
                if error.permanent and error.status != 400:
                    self.discord_paused = True
                    LOG.error("Discord sending paused; correct configuration and restart")
                    break
                if error.status != 400:
                    self.discord.not_before = max(self.discord.not_before, self.clock() + delay)
                    break
            else:
                with self.state.db:
                    self.state.sent(row["id"], message_id, self.clock())
                LOG.info("Delivered %s", row["id"])
            if self.clock() < self.discord.not_before:
                break

    def cleanup(self):
        now = self.clock()
        rows = self.state.cleanup_rows(now)
        to_check = [row for row in rows if not row["cleanup"] and row["sent_at"] + RETENTION > now]
        current = None
        if to_check and now >= self.source.not_before:
            try:
                current = self.source.lookup([row["id"] for row in to_check])
            except RemoteError as error:
                self._source_failure(error)
        for row in rows:
            remove = row["cleanup"] or row["sent_at"] + RETENTION <= now
            if not remove and current is not None:
                post = current.get(row["id"])
                remove = post is None or post.removed
            if not remove:
                if current is not None:
                    with self.state.db:
                        self.state.db.execute(
                            "UPDATE posts SET check_at=? WHERE id=?",
                            (min(now + 900, row["sent_at"] + RETENTION), row["id"]),
                        )
                continue
            # Persist deletion intent before the remote call, for crash recovery.
            with self.state.db:
                self.state.db.execute("UPDATE posts SET cleanup=1 WHERE id=?", (row["id"],))
            if self.discord_paused or self.clock() < self.discord.not_before:
                continue
            try:
                self.discord.delete(row["message_id"])
            except RemoteError as error:
                self.had_error = True
                delay = backoff(row["cleanup_attempts"] + 1, error.delay)
                with self.state.db:
                    self.state.db.execute(
                        "UPDATE posts SET cleanup_attempts=cleanup_attempts+1,check_at=?,last_error=? WHERE id=?",
                        (self.clock() + delay, str(error), row["id"]),
                    )
                LOG.error("Cleanup %s: %s", row["id"], error)
                if error.permanent:
                    self.discord_paused = True
                    LOG.error("Discord cleanup paused; correct configuration and restart")
                self.discord.not_before = max(self.discord.not_before, self.clock() + delay)
                break
            else:
                with self.state.db:
                    self.state.db.execute(
                        "UPDATE posts SET message_id=NULL,cleanup=0,last_error=NULL WHERE id=?",
                        (row["id"],),
                    )
                LOG.info("Removed retained message for %s", row["id"])

    def tick(self):
        self.had_error = False
        try:
            self.discover()
            self.cleanup()
            self.deliver()
        finally:
            with self.state.db:
                self.state.set("reddit_not_before", self.source.not_before)
                self.state.set("discord_not_before", self.discord.not_before)
        return not self.had_error

    def health(self):
        last = self.state.get("last_poll")
        oldest = self.state.db.execute(
            "SELECT MIN(created) FROM posts WHERE status='pending'"
        ).fetchone()[0]
        cleanup = self.state.db.execute("SELECT COUNT(*) FROM posts WHERE cleanup=1").fetchone()[0]
        LOG.info(
            "Health poll_age_s=%s oldest_pending_age_s=%s queue=%s cleanup_pending=%d discord_paused=%s reddit_wait_s=%d discord_wait_s=%d",
            round(self.clock() - last) if last is not None else "never",
            round(self.clock() - oldest) if oldest is not None else "none",
            self.state.summary(),
            cleanup,
            self.discord_paused,
            max(0, round(self.source.not_before - self.clock())),
            max(0, round(self.discord.not_before - self.clock())),
        )
