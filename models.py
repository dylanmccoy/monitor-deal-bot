"""Minimal Reddit post data; no author or post body is retained."""

import math
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Post:
    id: str
    title: str
    flair: str
    created: float
    reddit_url: str
    url: str = ""
    thumbnail: str = ""
    removed: bool = False
    discussion: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_reddit(cls, data: dict):
        if not isinstance(data, dict):
            raise ValueError("Invalid post object")
        name = data.get("name") or f"t3_{data.get('id', '')}"
        title = data.get("title")
        created = data.get("created_utc")
        permalink = data.get("permalink")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"t3_[a-z0-9]+", name)
            or not isinstance(title, str)
            or not title
            or isinstance(created, bool)
            or not isinstance(created, (int, float))
            or not math.isfinite(created)
            or not 0 < created < 253402300799
            or not isinstance(permalink, str)
            or not re.fullmatch(r"/r/[A-Za-z0-9_]+/comments/[a-z0-9]+(?:/[^\s?#]*)?", permalink)
        ):
            raise ValueError("Invalid required post fields")

        def text(key):
            value = data.get(key)
            return value if isinstance(value, str) else ""

        return cls(
            name,
            title,
            text("link_flair_text"),
            float(created),
            "https://www.reddit.com" + permalink,
            text("url"),
            text("thumbnail"),
            bool(data.get("removed_by_category") or data.get("banned_by"))
            or title.strip().lower() in ("[removed]", "[deleted]"),
            bool(data.get("is_self")),
        )
