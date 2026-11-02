"""Configuration shared by the CLI and worker."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def positive(value: str, name: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


def webhook_id(url: str) -> str:
    try:
        parts = urlsplit(url)
        match = re.fullmatch(r"/api(?:/v\d+)?/webhooks/(\d+)/([A-Za-z0-9_-]+)", parts.path)
        valid = (
            parts.scheme == "https"
            and parts.hostname == "discord.com"
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and match
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("DISCORD_WEBHOOK_URL must be a Discord HTTPS webhook URL")
    return match[1]


@dataclass(frozen=True)
class Config:
    webhook_url: str = field(default="", repr=False)
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    user_agent: str = ""
    subreddit: str = "buildapcsales"
    poll_interval: float = 60
    db_path: Path = ROOT / "data/deals.sqlite3"
    max_age: float = 86400
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    keyword_mode: str = "any"
    monitor_flairs: tuple[str, ...] = ("Monitor", "Monitors")
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: Path = ROOT / ".env"):
        load_dotenv(env_file, override=False)

        def keywords(name, default=""):
            return tuple(v.strip() for v in os.getenv(name, default).split(",") if v.strip())

        db_path = Path(os.getenv("STATE_DB_PATH", "./data/deals.sqlite3")).expanduser()
        config = cls(
            webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
            client_id=os.getenv("REDDIT_CLIENT_ID", "").strip(),
            client_secret=os.getenv("REDDIT_CLIENT_SECRET", "").strip(),
            refresh_token=os.getenv("REDDIT_REFRESH_TOKEN", "").strip(),
            user_agent=os.getenv("REDDIT_USER_AGENT", "").strip(),
            subreddit=os.getenv("SUBREDDIT", "buildapcsales").strip(),
            poll_interval=positive(
                os.getenv("POLL_INTERVAL_SECONDS", "60"), "POLL_INTERVAL_SECONDS"
            ),
            db_path=(ROOT / db_path).resolve(),
            max_age=positive(os.getenv("MAX_POST_AGE_HOURS", "24"), "MAX_POST_AGE_HOURS") * 3600,
            include=keywords("INCLUDE_KEYWORDS"),
            exclude=keywords("EXCLUDE_KEYWORDS"),
            keyword_mode=os.getenv("KEYWORD_MATCH_MODE", "any").lower().strip(),
            monitor_flairs=keywords("MONITOR_FLAIRS", "Monitor,Monitors"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip(),
        )
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", config.subreddit):
            raise ValueError("SUBREDDIT must be a single subreddit name without r/")
        if config.keyword_mode not in ("any", "all"):
            raise ValueError("KEYWORD_MATCH_MODE must be any or all")
        if config.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")
        return config

    def require_reddit(self):
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET for approved OAuth access"
            )
        if (
            not self.user_agent
            or "YOUR_USERNAME" in self.user_agent
            or "\n" in self.user_agent
            or "\r" in self.user_agent
        ):
            raise ValueError("Set REDDIT_USER_AGENT with your app identity and Reddit username")

    def require_webhook(self):
        return webhook_id(self.webhook_url)
