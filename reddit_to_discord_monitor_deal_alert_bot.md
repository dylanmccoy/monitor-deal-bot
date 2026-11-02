# Plan: Monitor deal alerts from Reddit to Discord

Revised November 2, 2026. The worker is implemented in this project and verified with offline tests. See [README.md](README.md) for setup and current behavior. Live verification and deployment still require locally configured Discord and approved Reddit credentials.

## 1. Goal and scope

Run a small Python worker that watches new posts in `r/buildapcsales` and sends monitor deals to one Discord channel through an incoming webhook.

Default behavior:

- Alert on **all monitor deals**; optional keywords narrow the results.
- Poll every 60 seconds. Target delivery within one polling interval plus request time during normal operation; this is polling, not an instant event subscription.
- Suppress existing posts on the first successful startup.
- Preserve state across restarts and retry failed deliveries.
- Catch up on recent posts after downtime, with a 24-hour maximum post age to avoid stale alerts.
- Treat a deal post as a lead, not proof of current stock, price, or deal quality.

Version one needs no website, Discord bot account, Discord gateway connection, retailer scraping, LLM, or price-history service. Start with one source and one destination.

## 2. Correct the Reddit access assumption first

The original proposal assumed public `search.json` could be polled indefinitely without credentials. That is not a dependable deployment plan. Reddit currently requires approved API access and OAuth; its documentation says unauthenticated traffic will be blocked. A custom User-Agent alone does not grant access. See the [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) and [Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki).

Before implementing the live Reddit adapter:

1. Confirm existing approved access, or request access for this personal, read-only monitoring use case through Reddit's current developer support process.
2. Use the OAuth flow allowed for the approved application; support token renewal and a descriptive User-Agent identifying the application and contact username.
3. Validate an authenticated listing request from the intended host.

Approval and credentials are a prerequisite for live Reddit monitoring, not for building and testing the rest of the worker. If access is unavailable, keep the source interface small so an explicitly supported feed from another deal provider can be substituted later. Do not assume RSS or a third-party scraper resolves Reddit access restrictions.

Use `requests` for the small HTTP client and `python-dotenv` for local configuration; SQLite is in Python's standard library. PRAW is optional, not a requirement. Record tested dependency versions when implementing rather than copying old lower bounds from this plan.

## 3. Architecture and files

```text
Authenticated Reddit /new listing
             |
             v
Normalize -> classify monitor -> optional filters
             |
             v
SQLite transaction: record post + queue eligible notification
             |
             v
Discord webhook sender -> persist acknowledgement / schedule retry
```

Keep one worker process, with independent fetch and delivery schedules so a Discord outage does not prevent discovering posts. Use due times rather than long sleeps inside retry handlers.

```text
monitor-deal-bot/
├── README.md                   # Setup, commands, deployment, recovery
├── .env.example                # Placeholder configuration only
├── .gitignore                  # .env, .venv/, data/, caches
├── requirements.txt            # Tested runtime dependencies
├── requirements-dev.txt        # Test dependencies
├── monitor_deals.py            # CLI and worker orchestration
├── reddit_source.py            # OAuth, listing pagination, normalization
├── filters.py                  # Monitor classification and keyword matching
├── discord_webhook.py          # Payloads, acknowledgement, rate limits
├── state.py                    # SQLite schema and transactions
├── tests/                      # Fixtures and mocked HTTP tests
├── data/                       # Persistent SQLite database; never committed
└── deploy/dealwatcher.service   # Optional Linux service
```

## 4. Configuration

Example `.env.example`:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN

# Values issued for the approved Reddit application.
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=linux:personal-monitor-deal-watcher:v1.0 (by /u/YOUR_USERNAME)
# Add a refresh-token setting if required by the approved OAuth flow.

SUBREDDIT=buildapcsales
POLL_INTERVAL_SECONDS=60
STATE_DB_PATH=./data/deals.sqlite3
MAX_POST_AGE_HOURS=24

# Empty means all monitor deals. Comma-separated, case-insensitive.
INCLUDE_KEYWORDS=
EXCLUDE_KEYWORDS=
KEYWORD_MATCH_MODE=any
LOG_LEVEL=INFO
```

Validate required credentials, HTTPS webhook URL, positive numeric settings, and keyword mode on startup. Resolve relative paths against the project directory, not the caller's working directory. Environment variables should take precedence over `.env` values.

Keep the webhook URL and OAuth credentials out of source control and logs, including HTTP exception strings that may contain the webhook token. Use separate HTTP sessions for Reddit and Discord so authorization headers cannot cross services.

## 5. Discover and classify deals

Use the authenticated **new posts listing**, not search results:

```text
GET https://oauth.reddit.com/r/buildapcsales/new?limit=100&raw_json=1
Authorization: Bearer <access_token>
```

The listing supports `after` pagination and a maximum page size of 100. Filtering locally avoids making search indexing part of notification latency. See the [Reddit listing API reference](https://www.reddit.com/dev/api/#GET_new).

On each poll:

1. Validate HTTP status and the listing structure before accepting a response. An HTML block page, malformed JSON, or missing `data.children` is a failure, not an empty listing.
2. Page backward until reaching the previous completed poll's start time minus a ten-minute overlap, or the maximum post-age boundary, whichever is newer. Do not stop just because one seen ID appears.
3. Cap work at ten pages per cycle. If that cap prevents covering the required window, log a coverage gap and retain the old checkpoint. Persist a catch-up cursor and its target window so subsequent cycles can resume older pages while still checking the newest page for fresh deals. If a cursor becomes invalid, restart the bounded scan and report the gap. Listing history is finite; complete recovery after a long outage is not guaranteed.
4. Deduplicate by Reddit fullname, such as `t3_abc123`, and process eligible new posts oldest first.

Classification rules:

- Recognize monitor-specific flair using configurable, normalized exact matches, initially `Monitor` and `Monitors`.
- Also accept a title beginning with `[Monitor]` or `[Monitors]`, covering missing or changed flair.
- Reject monitor arms, mounts, stands, accessories, discussion posts, removed/deleted posts, and posts explicitly marked expired or sold out. Accessory-only titles must not qualify through a generic `monitor` substring.
- Maintain a small fixture set of real title/flair shapes; review ambiguous cases during dry runs.
- Apply optional include/exclude filters only after monitor classification. Exclusions win. `any` means at least one include keyword; `all` means every include keyword. An empty include list accepts every classified monitor deal.
- Normalize common spacing and punctuation variations, such as `240 Hz`/`240Hz` and `mini LED`/`mini-LED`. Document that title keywords cannot guarantee exact specifications or prices.

Keep the original title for display. Do not invent a price or retailer when the title/link does not supply one. Recheck current post availability and expired status before sending a delayed retry. Edits and reposts are outside version one's deduplication promise: one Reddit post gets one notification; a new post ID can alert again.

## 6. Startup, persistence, and delivery semantics

Use SQLite rather than an in-memory `seen_post_ids` set. Store:

- Worker metadata: schema version, initialization time, completed discovery checkpoint, and any in-progress catch-up cursor/window.
- Post identity and creation time, with a unique constraint on source + post ID.
- Notification state: `suppressed`, `skipped`, `pending`, `sent`, `expired`, or `failed`.
- For pending items: minimal payload, attempt count, next attempt time, and sanitized last error.
- For sent items: Discord message ID and send time, also used for content cleanup.

**First run:** retry until a valid initial listing is available. In one transaction, seed its IDs as suppressed and save the initialization boundary at that successful poll's start time. Any subsequently encountered post older than that boundary is also suppressed. Do not announce active monitoring or send notifications if initialization fails. A valid empty listing can initialize the worker.

**Normal operation and restart:** reuse the database and boundary. Persist each eligible post as pending before trying Discord. Advance the discovery checkpoint only after the complete required listing window has been fetched and recorded durably. A fetch error must not discard pending deliveries, and a failed send must not become `sent`.

**Backlog:** retry pending items after restart, but expire anything older than `MAX_POST_AGE_HOURS`. Store reasons for skipped/expired items without retaining their full content. Changing filters affects newly discovered posts; replay is a separate explicit operation, not a consequence of restarting.

Enforce a single active worker per database with a process lock. Treat corrupt state or disk-write failures as visible errors; never silently replace the database and start over.

Delivery is durable retry with deduplication after confirmed success. It is **not exactly once**: Discord may accept a POST before the client times out or before the local acknowledgement is committed. A retry can then produce a duplicate. Document this narrow failure window instead of promising zero duplicates.

## 7. Discord webhook integration

Create an incoming webhook in the intended Discord text channel and store its URL locally. Start with a standard text channel to avoid forum/thread setup.

Each alert should contain:

- A short “New monitor deal” label.
- Original post title in an embed, truncated to Discord's field limits.
- A link to the Reddit discussion and an outbound deal link when it is a valid HTTP(S) URL.
- Original post timestamp in UTC and source subreddit.
- Optional valid thumbnail; omit placeholders and malformed URLs.

Use `allowed_mentions: {"parse": []}` and sanitize user-provided text and Markdown links. Do not request author information or reproduce post bodies; neither is necessary for a useful alert.

Execute the webhook with `wait=true` and persist the returned message ID before marking delivery successful. Discord documents that the default `wait=false` can hide a message that was not saved. See the [webhook reference](https://docs.discord.com/developers/resources/webhook).

Handle outcomes explicitly:

| Outcome | Action |
| --- | --- |
| Confirmed message response with ID | Commit `sent` and retain ID for cleanup |
| HTTP 429 | Respect `Retry-After` / JSON `retry_after` in seconds; schedule retry |
| Network error, timeout, or HTTP 5xx | Keep pending; exponential backoff with jitter |
| HTTP 400 | Mark that notification failed and log a sanitized validation error; continue other items |
| HTTP 401, 403, or 404 | Pause sending and report a configuration/access failure; retain pending items until expiry |

Inspect rate-limit headers; do not hard-code a fixed webhook throughput. See [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits). If the response is ambiguous, keep it retryable rather than pretending success.

Retain only necessary content. Reddit requires removal of deleted content, including copied titles and URLs. Track delivered message IDs, recheck retained source posts every 15 minutes, and delete associated webhook messages when source content is removed; expire copied content and delete its Discord message after 24 hours by default. Retry failed cleanup after restart and surface failures. Keep only minimal deduplication metadata afterward. This lifecycle applies to Discord copies as well as the local queue; see the [Data API guidance](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki).

## 8. Error handling and operations

- Use explicit connection/read timeouts for every request.
- For transient Reddit errors, use exponential backoff with jitter, initially 5 seconds and capped at 15 minutes. Respect longer server-provided waits and OAuth rate-limit headers.
- Refresh an expired OAuth token once; persistent authentication failures or forbidden access should produce a clear operational error rather than a rapid retry loop.
- Keep Reddit and Discord backoff schedules separate. Continue processing due work for the healthy service.
- Log initialization, successful poll time, fetched/classified counts, queue size, acknowledgements, retries, expired alerts, and coverage gaps. Avoid full Reddit payloads and titles in persistent logs.
- Emit a periodic health summary including time since last successful poll and oldest pending delivery. A running process alone does not prove healthy monitoring.
- Handle shutdown cleanly, commit state, and preserve pending work.

For Reddit's actual request allowance, use returned limits and the approved access terms; the poll interval is an application default, not a guarantee of access or quota.

## 9. Commands and deployment

Implement these explicit modes:

```bash
python monitor_deals.py --dry-run --once
python monitor_deals.py --test-webhook
python monitor_deals.py --once
python monitor_deals.py
```

- `--dry-run --once`: fetch and show classifications without sending messages or modifying production state. Require approved Reddit access for a live fetch; support fixtures for offline work.
- `--test-webhook`: send one synthetic, clearly labeled test notification, with no Reddit access or production queue changes.
- `--once`: one discovery cycle and a bounded pass over due deliveries, then exit with a meaningful status. On a fresh database it initializes quietly.
- No flags: continuous worker.

Document virtual environment setup, credentials, local execution, recovery from an invalid webhook, database backup, and persistent storage. Keep tests independent of real credentials.

For Linux hosting, use a virtual environment and an optional systemd service. Example for the current project location; adapt the paths and user on another host:

```ini
[Unit]
Description=Monitor deal alerts to Discord
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=dylan
WorkingDirectory=/home/dylan/projects/dylan/monitor-deal-bot
EnvironmentFile=/home/dylan/projects/dylan/monitor-deal-bot/.env
ExecStart=/home/dylan/projects/dylan/monitor-deal-bot/.venv/bin/python -u /home/dylan/projects/dylan/monitor-deal-bot/monitor_deals.py
Restart=on-failure
RestartSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
```

Install and enable the service only after local verification. Document `systemctl status dealwatcher` and `journalctl -u dealwatcher -f` for inspection.

Windows Downloads is the source of this plan, not a requirement to host on Windows. WSL works for development, but the worker stops when its host sleeps or shuts down. For continuous monitoring, choose an always-on Linux host, Raspberry Pi, or VPS with persistent disk. Native Windows hosting can use Task Scheduler with restart behavior instead of systemd.

## 10. Acceptance checks

Use mocked HTTP responses, a fake clock, and a temporary SQLite database for automated checks:

1. First successful initialization sends nothing; failed initialization does not permit a historical alert flood.
2. One new matching monitor post sends once; repeated polls and a normal restart do not resend it.
3. Empty keyword settings accept all monitors; accessories, expired posts, exclusions, and `any`/`all` behavior work as documented.
4. Multiple pages, overlapping polls, and more than 100 intervening posts are handled. Truncation is visible and does not incorrectly advance the checkpoint.
5. Discord 429/5xx/timeouts leave a durable pending item; restart resumes it; acknowledgement changes it to sent.
6. Expired backlog is skipped. Malformed JSON and missing listing fields do not count as successful polls.
7. Reddit failures do not block delivery retries; Discord failures do not block discovery. Permanent failures and disk errors are visible.
8. Payloads respect Discord limits, handle missing fields and unsafe Markdown, suppress mentions, and do not leak secrets into logs.
9. Dry run cannot consume production notifications. The synthetic webhook test does not mutate production history.
10. Source deletion and retention cleanup remove copied content, including Discord messages; cleanup failures survive restart.

Then perform a deliberate synthetic webhook test and a short live monitoring trial from the intended host. Do not clear production history to test delivery, and do not deliberately trigger platform rate limits. Verify the documented duplicate window through simulation rather than claiming exactly-once delivery.

## 11. Build order and completion

1. Validate approved Reddit access; separately implement the synthetic webhook test.
2. Build normalization/filtering with fixtures, SQLite state, and the queued sender with mocked failures.
3. Add live authenticated discovery, pagination, retention cleanup, and CLI modes.
4. Run the acceptance checks, then verify startup/restart and alert rendering on the intended host.
5. Configure the persistent worker and confirm health logs through several polling cycles.

The integration is complete when it quietly initializes, delivers new monitor posts, survives a restart and a temporary webhook failure, expires stale backlog, and visibly reports source or destination failures. Live operation still depends on Reddit access, a configured Discord webhook, and an awake host with persistent storage.
