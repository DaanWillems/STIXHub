import threading
from collections.abc import Callable
from typing import TypeVar

import httpx

from collectors.platform_client import PlatformClient
from collectors.retry import RetriesExhausted, with_retries
from models.domain import CollectorResponse

T = TypeVar("T")

# Registration happens while the HTTP server may still be starting, so it gets
# a more generous retry budget than a regular step.
REGISTRATION_ATTEMPTS = 10


class RegistrationError(RuntimeError):
    """The platform refused or could not complete a registration."""


class Retrier:
    """Retries transient failures with back-off; stops waiting on shutdown."""

    def __init__(
        self,
        name: str,
        max_retries: int,
        stop: threading.Event,
        backoff_base: float = 1.0,
    ) -> None:
        self._name = name
        self._max_retries = max_retries
        self._stop = stop
        self._backoff_base = backoff_base

    def __call__(
        self, fn: Callable[[], T], description: str, attempts: int | None = None
    ) -> T:
        return with_retries(
            fn,
            description=f"[{self._name}] {description}",
            max_retries=attempts or self._max_retries,
            wait=self._stop.wait,
            base_delay=self._backoff_base,
        )


def load_cursor(name: str, raw: str | None, parse: Callable[[str], T]) -> T | None:
    """Parse a stored cursor; a malformed one is a registration error."""
    if raw is None:
        return None
    try:
        return parse(raw)
    except (ValueError, KeyError, TypeError) as exc:
        raise RegistrationError(
            f"'{name}' has a stored cursor it can't read ({raw!r}); was the "
            "collector renamed?"
        ) from exc


def register(
    platform: PlatformClient, retry: Retrier, name: str, bucket: str
) -> CollectorResponse:
    """Register ``name`` for ``bucket``; raises ``RegistrationError`` on failure."""
    try:
        return retry(
            lambda: platform.register(name, bucket),
            f"register '{name}'",
            REGISTRATION_ATTEMPTS,
        )
    except (RetriesExhausted, httpx.HTTPError) as exc:
        detail = (
            f"HTTP {exc.response.status_code} {exc.response.text}"
            if isinstance(exc, httpx.HTTPStatusError)
            else str(exc)
        )
        raise RegistrationError(
            f"'{name}' could not register with the platform "
            f"(bucket '{bucket}'): {detail}"
        ) from exc
