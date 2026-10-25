"""Timezone helpers.

Postgres returns aware datetimes for `TIMESTAMPTZ`; SQLite (used by the test
suite) returns naive ones. Everything in the domain is UTC, so normalise on read
rather than scattering tzinfo checks through the services.

Purpose:    one place that turns any datetime into aware UTC and gives "now".
Why needed: without it, timer math and metrics would break (naive vs aware
            datetimes cannot be compared) whenever the test DB and Postgres
            disagree about timezones.
Contains:
    as_utc()  - normalise a datetime read from the DB so comparisons are safe.
    utcnow()  - the single clock every service uses for timestamps.
Used by:    auth/service, common/events, evaluation/worker, notifications/service,
            realtime/livekit, realtime/protocol, session/service and the
            session lifecycle tests.
Depends on: nothing inside the project (stdlib `datetime` only).
"""

from datetime import UTC, datetime  # UTC constant and the datetime type


def as_utc(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime (naive values are assumed to be UTC)."""
    # Naive values come from SQLite; the domain stores UTC, so just label them.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    # Aware values (Postgres) may carry another offset; convert to UTC.
    return value.astimezone(UTC)


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)
