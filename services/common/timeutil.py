"""Timezone helpers.

Postgres returns aware datetimes for `TIMESTAMPTZ`; SQLite (used by the test
suite) returns naive ones. Everything in the domain is UTC, so normalise on read
rather than scattering tzinfo checks through the services.
"""

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)
