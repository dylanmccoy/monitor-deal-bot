"""Safe, bounded HTTP error descriptions and retry timing."""

import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class RemoteError(Exception):
    def __init__(self, service: str, reason: str, *, status=None, delay=0, permanent=False):
        # Never include response bodies, URLs, or underlying requests exceptions.
        super().__init__(f"{service}: {reason}" + (f" (HTTP {status})" if status else ""))
        self.status = status
        self.delay = delay
        self.permanent = permanent


def seconds(value) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def retry_after(response) -> float:
    value = response.headers.get("Retry-After")
    delay = seconds(value)
    if value and not delay:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        delay = max(delay, seconds(body.get("retry_after")))
    return delay


def backoff(attempt: int, minimum: float = 0) -> float:
    base = min(900, 5 * 2 ** min(max(attempt - 1, 0), 8))
    return max(minimum, min(900, base * random.uniform(1, 1.25)))
