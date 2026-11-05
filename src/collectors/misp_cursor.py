import json
from dataclasses import dataclass, field

SECONDS_PER_DAY = 86400


class MispCursorError(ValueError):
    """Raised when a stored cursor string cannot be parsed."""


@dataclass(frozen=True)
class MispCursor:
    """Position of a MISP collector: the highest listed ``publish_timestamp`` seen.

    ``recent`` maps the uuid of events handled inside the overlap window to the
    listed ``publish_timestamp`` they were handled at, so that re-listing them
    because of the overlap (or same-second events) does not resubmit them.
    """

    publish_timestamp: int
    recent: dict[str, int] = field(default_factory=dict)

    @classmethod
    def initial(
        cls,
        now: int,
        initial_lookback_days: int | None,
        overlap_seconds: int = 0,
    ) -> "MispCursor":
        """Cursor for a first run, whose ``window_start`` is ``now - lookback``.

        Without a lookback the window is unbounded (``window_start`` is 0).
        """
        if initial_lookback_days is None:
            return cls(publish_timestamp=0)
        start = max(0, now - initial_lookback_days * SECONDS_PER_DAY)
        # window_start() subtracts the overlap again, so add it back here.
        return cls(publish_timestamp=start + overlap_seconds)

    @classmethod
    def parse(cls, raw: str) -> "MispCursor":
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise MispCursorError(f"cursor is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise MispCursorError("cursor must be a JSON object")
        ts = data.get("publish_timestamp")
        if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
            raise MispCursorError("cursor publish_timestamp must be a non-negative integer")
        recent = data.get("recent", {})
        if not isinstance(recent, dict):
            raise MispCursorError("cursor recent must be an object")
        for uuid, value in recent.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise MispCursorError(f"cursor recent[{uuid!r}] must be an integer")
        return cls(publish_timestamp=ts, recent=dict(recent))

    def serialize(self) -> str:
        return json.dumps(
            {"publish_timestamp": self.publish_timestamp, "recent": self.recent},
            sort_keys=True,
        )

    def window_start(self, overlap_seconds: int) -> int:
        return max(0, self.publish_timestamp - overlap_seconds)

    def should_skip(self, uuid: str, publish_timestamp: int) -> bool:
        """True if this exact version of the event was already handled."""
        return self.recent.get(uuid) == publish_timestamp

    def advance(self, uuid: str, publish_timestamp: int, overlap_seconds: int) -> "MispCursor":
        """Return a cursor that has handled the event; never moves backwards.

        ``recent`` is pruned to entries inside the new overlap window.
        """
        new_ts = max(self.publish_timestamp, publish_timestamp)
        floor = max(0, new_ts - overlap_seconds)
        recent = {**self.recent, uuid: publish_timestamp}
        return MispCursor(
            publish_timestamp=new_ts,
            recent={u: t for u, t in recent.items() if t >= floor},
        )
