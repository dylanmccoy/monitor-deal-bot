# Monitor deal alerts

A small Python worker that watches `r/buildapcsales` and sends new monitor deals to a Discord incoming webhook. Defaults to all monitors, checks every 60 seconds, and keeps delivery history and pending retries in SQLite.

The implementation and offline tests are ready. Live verification and deployment require your Discord webhook and approved Reddit OAuth credentials; neither is included. Python 3.12+ is supported; development verification used Python 3.13.

## Set up

From this project directory on Linux or WSL:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

If Python's `venv` module is unavailable, `uv venv .venv` and `uv pip install --python .venv/bin/python -r requirements.txt` work too. A virtual environment has already been created in the current development workspace.

Edit `.env` locally. Environment variables override `.env` settings. The file is ignored by Git; keep secrets out of chat and commits.

1. In the target Discord **text channel**, open channel settings → Integrations → Webhooks, create a webhook, and copy its URL into `DISCORD_WEBHOOK_URL`.
2. For live monitoring, fill in `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and your descriptive `REDDIT_USER_AGENT` using an approved application.
3. Leave `REDDIT_REFRESH_TOKEN` empty for application-only `client_credentials` OAuth. If your approved application instead uses an existing refresh token, supply it here with access to the `read` scope. Access tokens are renewed automatically. This worker does not perform the interactive authorization-code exchange.

Reddit requires [approved API access](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) and [OAuth authentication](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki), including for this read-only use case. The client supports confidential web/script applications using the [documented OAuth grants](https://github.com/reddit-archive/reddit/wiki/OAuth2#application-only-oauth); it does not support unauthenticated JSON or installed-app grants.

## Verify Discord independently

Only `DISCORD_WEBHOOK_URL` is needed:

```bash
.venv/bin/python monitor_deals.py --test-webhook
```

This explicitly sends one synthetic message, waits for Discord's acknowledgement, and logs its message ID. It does not access Reddit or open the production database. The synthetic message remains in the channel until you remove it.

## Preview matches

Try the bundled synthetic fixture without any credentials or network access:

```bash
.venv/bin/python monitor_deals.py --dry-run --once --fixture tests/fixtures/monitor_posts.json
```

With Reddit configured, preview the newest live page:

```bash
.venv/bin/python monitor_deals.py --dry-run --once
```

Dry runs print one JSON line per post with its classification and title. They never send Discord messages or open production state. They show matching behavior, not whether an item would be suppressed by existing delivery history. Fixture data is synthetic, not an actual offer. `--dry-run` is always a one-shot preview, even without `--once`.

## Start watching

```bash
.venv/bin/python monitor_deals.py --once
.venv/bin/python monitor_deals.py
```

The first successful poll seeds history quietly. Later runs reuse `data/deals.sqlite3`. A fresh database intentionally suppresses existing deals, so restarting with the same state file matters. Initialization failures are retried and cannot trigger a history flood.

The continuous worker logs polls, delivery acknowledgements, retries, coverage gaps, and a health summary every five minutes. Use Ctrl+C or SIGTERM to stop; pending work stays in SQLite. One active worker is allowed per database.

`--once` performs a bounded discovery pass, up to 20 due deliveries, and a bounded cleanup pass. Exit code 0 means the pass completed without a new error, discovery actually ran, and no pending deliveries/deletions remain. Exit code 1 means configuration, fetching, delivery, cleanup, pagination coverage, or outstanding work needs attention. Argument errors return 2. It does not wait for future retries or drain a large backlog. Use the continuous worker for normal operation.

## Filters

Leave both keyword lists empty for all monitors. Example settings:

```dotenv
INCLUDE_KEYWORDS=OLED,1440p,4K
EXCLUDE_KEYWORDS=refurbished,open box
KEYWORD_MATCH_MODE=any
```

`any` accepts at least one include keyword; `all` requires every include keyword. Exclusions always win. Keyword matching ignores case and normalizes punctuation plus common forms such as `240 Hz`/`240Hz` and `mini LED`/`mini-LED`. It matches whole words/phrases, not arbitrary substrings. An unknown spec in a title cannot be inferred.

Monitor flair (`Monitor`/`Monitors`, configurable with `MONITOR_FLAIRS`) or a leading `[Monitor]`/`[Monitors]` title tag is required. Accessory-only listings, self/discussion posts, and explicitly unavailable deals are rejected. Classification is heuristic; inspect dry-run output for unusual flair or title formats. The bot does not verify retailer inventory, discounts, or product specifications.

Configuration also includes `POLL_INTERVAL_SECONDS`, `SUBREDDIT`, `MAX_POST_AGE_HOURS`, `STATE_DB_PATH`, and `LOG_LEVEL`. Paths resolve relative to the project directory. A database is bound to its subreddit and webhook ID to prevent accidentally sending a backlog to another destination. Rotating the token for the same webhook ID can reuse state; changing the webhook ID requires a separate state file.

## Delivery and recovery

- New posts are recorded before sending; a confirmed Discord message ID changes a pending notification to sent. Transient errors retry with exponential backoff and jitter. Discord/server retry headers are respected and cooldowns survive restart.
- Successful notifications are deduplicated by Reddit post ID across polls and restarts. A timeout after Discord accepts a message, or a crash before recording acknowledgement, can cause a duplicate on retry. Exactly-once delivery is not guaranteed.
- Recent downtime is recovered using paginated new listings with an overlap. The default maximum post age is 24 hours. Pagination is bounded to ten pages per cycle; an incomplete scan saves its cursor and reports a coverage gap. The available Reddit listing history limits recovery after long outages.
- A retried delivery checks whether its source post still exists and is available. During Reddit failures, already queued first attempts may still send; retries requiring revalidation wait for Reddit recovery. A Discord outage does not disable discovery.
- Discord 400 marks the offending notification failed. Authorization/access errors pause Discord sending until you fix the configuration and restart. Correcting OAuth access is retried after a longer backoff. The health log shows remaining service cooldowns, which are preserved across restarts.
- Full local payloads are removed after successful delivery, rejection, or expiry. Delivered deal messages are checked for source removal every 15 minutes and deleted from Discord after 24 hours by default. Deletion intent and retries are durable. Source downtime delays removal checks; Discord downtime delays deletion. Unacknowledged sends can leave an untracked duplicate that must be removed manually. The synthetic webhook test is not part of this lifecycle.
- Filter changes apply to newly discovered IDs. Old skipped/failed items are not replayed automatically. New post IDs can alert again even if they describe the same offer.

If no alerts arrive, inspect the health log's last-poll age, queue state, cooldowns, and paused flag. Run a dry preview to check filters and the synthetic webhook test to check Discord. The worker never logs raw HTTP error bodies or webhook URLs. Do not delete production state to test notifications.

To back up state, stop the worker cleanly and copy its SQLite file, then restart. To restore, stop the worker and restore a compatible backup at `STATE_DB_PATH`; an older backup may replay deliveries confirmed after the backup. Corrupt or unsupported databases cause an error rather than an automatic reset. Keep `.env` backed up separately and privately.

## Run continuously on Linux

Review `deploy/dealwatcher.service` and adjust its user/project paths for the host. Then, after the synthetic test and a live trial succeed:

```bash
sudo install -m 644 deploy/dealwatcher.service /etc/systemd/system/dealwatcher.service
sudo systemctl daemon-reload
sudo systemctl enable --now dealwatcher
systemctl status dealwatcher
journalctl -u dealwatcher -f
```

The service uses this project's virtual environment and `.env`, restarts on failure, and persists its database under `data/`. Only one copy should run against that file. These commands have **not** been run as part of implementation.

The host must remain awake with network access and persistent disk. WSL stops working as a monitor when Windows sleeps or shuts down; an always-on Linux host, Raspberry Pi, or VPS is appropriate for continuous operation. Native Windows users can use Task Scheduler and `.venv\Scripts\python.exe` with the project as the working directory; the process lock supports Windows, but Windows deployment has not been exercised here.

## Development checks

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The tests block real HTTP, use temporary SQLite databases and a fake clock, and cover initialization, restarts, keyword behavior, pagination, durable retry, platform cooldowns, safe payloads, source deletion, cleanup recovery, independent failures, and the ambiguous delivery window. They do not require credentials or send real messages. Direct dependencies are pinned to the versions used for development verification.

The original design and its access prerequisites are in [the plan](reddit_to_discord_monitor_deal_alert_bot.md).
