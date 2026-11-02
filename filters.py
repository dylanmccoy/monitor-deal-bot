"""Deterministic monitor classification and optional title filters."""

import re
import unicodedata

from models import Post


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(\d)\s+(hz)\b", r"\1\2", text)
    return re.sub(r"[\W_]+", " ", text).strip()


def contains(text: str, keyword: str) -> bool:
    needle = normalized(keyword)
    return bool(needle) and f" {needle} " in f" {normalized(text)} "


def unavailable(post: Post) -> bool:
    return post.removed or bool(
        re.search(
            r"\b(expired|sold\s*out|out\s*of\s*stock|oos)\b",
            normalized(post.flair) + " " + normalized(post.title),
        )
    )


def classify(post: Post, config) -> tuple[bool, str]:
    if unavailable(post):
        return False, "unavailable"
    if post.discussion:
        return False, "discussion"
    tagged = bool(re.match(r"^\s*\[monitors?\]", post.title, re.I))
    if not tagged and normalized(post.flair) not in {normalized(v) for v in config.monitor_flairs}:
        return False, "not_monitor"
    title = re.sub(r"^\s*\[monitors?\]\s*", "", post.title, flags=re.I)
    if re.search(r"\bmonitors?\s+(arms?|mounts?|stands?|accessories|risers?)\b", normalized(title)):
        return False, "accessory"
    if any(contains(title, kw) for kw in config.exclude):
        return False, "excluded_keyword"
    if config.include:
        matches = [contains(title, kw) for kw in config.include]
        if not (all(matches) if config.keyword_mode == "all" else any(matches)):
            return False, "missing_keyword"
    return True, "monitor"
