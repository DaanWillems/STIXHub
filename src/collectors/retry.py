import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CollectorStopped(Exception):
    """Raised when a stop was requested while the collector was waiting."""


class RetriesExhausted(Exception):
    """Raised when a transient error persisted for ``max_retries`` attempts."""


def is_transient(exc: BaseException) -> bool:
    """Network errors/timeouts, HTTP 5xx and 429 are transient; everything else is not."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status == 429
    return False


def retry_after_seconds(exc: BaseException) -> float | None:
    """Return the ``Retry-After`` delay of a 429 response, if present and valid.

    Both the delta-seconds and HTTP-date forms are understood. Negative or
    unparseable values yield ``None``.
    """
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 429:
        return None
    value = exc.response.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if math.isnan(seconds) or seconds < 0:
        return None
    return seconds


def with_retries(
    fn: Callable[[], T],
    *,
    description: str,
    max_retries: int,
    wait: Callable[[float], bool],
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> T:
    """Call ``fn``, retrying transient failures with exponential back-off.

    ``fn`` is attempted at most ``max_retries`` times; the delay between
    attempts is ``base_delay * 2**n`` capped at ``max_delay``; a 429 response's
    ``Retry-After`` replaces that delay (also capped at ``max_delay``). ``wait(delay)``
    sleeps and returns ``True`` if a stop was requested (``threading.Event.wait``
    semantics), in which case :class:`CollectorStopped` is raised.

    Permanent errors (e.g. HTTP 4xx) are re-raised immediately.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc):
                raise
            if attempt == max_retries:
                raise RetriesExhausted(
                    f"{description} failed after {max_retries} attempts: {exc!r}"
                ) from exc
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            retry_after = retry_after_seconds(exc)
            if retry_after is not None:
                delay = min(retry_after, max_delay)
            logger.warning(
                "%s failed (attempt %d/%d): %r; retrying in %.1fs",
                description,
                attempt,
                max_retries,
                exc,
                delay,
            )
            if wait(delay):
                raise CollectorStopped from exc
    raise ValueError("max_retries must be at least 1")
