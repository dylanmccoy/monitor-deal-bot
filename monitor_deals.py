"""Monitor deal worker CLI. Run --help for independent verification modes."""

import argparse
import json
import logging
import signal
import sqlite3
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from config import Config
from discord_webhook import DiscordWebhook
from filters import classify
from http_utils import RemoteError
from reddit_source import RedditSource, parse_listing
from state import State, worker_lock
from worker import Worker

LOG = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Send new r/buildapcsales monitor deals to a Discord webhook"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Show classifications without sending or opening production state",
    )
    modes.add_argument(
        "--test-webhook",
        action="store_true",
        help="Send one synthetic test message; requires only the Discord webhook URL",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run one bounded discovery/delivery pass"
    )
    parser.add_argument("--fixture", type=Path, help="Reddit listing JSON for an offline dry run")
    args = parser.parse_args(argv)
    if args.fixture and not args.dry_run:
        parser.error("--fixture requires --dry-run")
    if args.test_webhook and args.once:
        parser.error("--test-webhook is already a one-shot operation")
    try:
        config = Config.load()
        logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(message)s")
        # Third-party HTTP debug logging may include tokens in request URLs.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)
        with ExitStack() as stack:
            if args.test_webhook:
                config.require_webhook()
                discord = DiscordWebhook(config.webhook_url)
                stack.callback(discord.close)
                message_id = discord.send(
                    {
                        "content": "Monitor deal watcher test: Discord webhook delivery is working. This is a synthetic test, not a deal.",
                        "allowed_mentions": {"parse": []},
                    }
                )
                LOG.info("Synthetic webhook test confirmed, message ID %s", message_id)
                return 0
            if args.fixture:
                try:
                    posts, _ = parse_listing(json.loads(args.fixture.read_text()))
                except (OSError, ValueError):
                    raise ValueError("Could not read fixture as Reddit listing JSON") from None
            else:
                config.require_reddit()
                source = RedditSource(config)
                stack.callback(source.close)
                if args.dry_run:
                    posts, after = source.listing()
                    if after:
                        LOG.info("Dry run shows only the newest listing page")
            if args.dry_run:
                for post in posts:
                    accepted, reason = classify(post, config)
                    print(
                        json.dumps(
                            {
                                "id": post.id,
                                "match": accepted,
                                "reason": reason,
                                "title": post.title,
                            },
                            ensure_ascii=True,
                        )
                    )
                return 0
            destination = config.require_webhook()
            discord = DiscordWebhook(config.webhook_url)
            stack.callback(discord.close)
            stack.enter_context(worker_lock(config.db_path))
            state = State(config.db_path)
            stack.callback(state.close)
            state.bind(config.subreddit, destination)
            worker = Worker(config, state, source, discord)
            stop = threading.Event()

            def shutdown(signum, frame):
                stop.set()

            for sig in (signal.SIGTERM, signal.SIGINT):
                previous = signal.signal(sig, shutdown)
                stack.callback(signal.signal, sig, previous)
            next_health = 0
            while not stop.is_set():
                cycle_started = time.time()
                healthy = worker.tick()
                if time.time() >= next_health:
                    worker.health()
                    next_health = time.time() + 300
                if args.once:
                    pending = state.summary().get("pending", 0)
                    cleanup = state.db.execute(
                        "SELECT COUNT(*) FROM posts WHERE cleanup=1"
                    ).fetchone()[0]
                    polled = state.get("last_poll", 0) >= cycle_started
                    return (
                        0
                        if healthy
                        and polled
                        and not pending
                        and not cleanup
                        and not worker.discord_paused
                        else 1
                    )
                stop.wait(1)
            LOG.info("Stopped; pending state preserved")
        return 0
    except (ValueError, RemoteError) as error:
        LOG.error("%s", error)
        return 1
    except (OSError, sqlite3.Error):
        LOG.error(
            "Local storage or process-lock failure; inspect file permissions and database integrity"
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
