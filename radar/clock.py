"""The one source of time. Tests and the demo freeze it; nothing else calls datetime.now()."""

from datetime import UTC, datetime

_frozen: datetime | None = None


def now() -> datetime:
    return _frozen if _frozen is not None else datetime.now(UTC)


def freeze(dt: datetime) -> None:
    global _frozen
    if dt.tzinfo is None:
        raise ValueError("freeze() needs a timezone-aware datetime")
    _frozen = dt.astimezone(UTC)


def unfreeze() -> None:
    global _frozen
    _frozen = None
