"""Trading-session arithmetic. It knows no market and refuses to guess one.

A scheduler asks four questions and they are all the same question wearing
different clothes: is the market open at instant ``T``, when does the current or
next session begin and end, how long may I sleep, and is today a weekend, a
holiday or a short day. Answering any of them correctly is entirely about
timezones -- a session is declared in local wall-clock time and consumed as UTC
instants, and the conversion between the two is not a constant.

**This module supplies the mechanism and never the policy.** There is no default
open, no default close, no holiday list, no early-close time, and no assumption
about which days are the weekend. Every one of those is data
:class:`MarketCalendar` requires at construction. A caller cannot obtain a
session definition by accident, because there is nothing to construct without
supplying one.

That refusal is not fastidiousness. A hardcoded ``09:30``/``16:00`` would be
right for one venue and silently wrong for every other, and a shipped US holiday
list is a fact about a particular year that rots in place -- it would keep
answering confidently long after it stopped being true, and the failure would
present as "the engine slept through a trading day", which nobody attributes to
a constant in a calendar module. Hours and holidays belong to whoever operates
the account; this file only does the arithmetic on them.

**Named ``market_calendar`` rather than ``calendar``** because the latter
shadows the stdlib module of that name for every importer in the package.

**Pure, and stdlib only.** No broker, no filesystem, no network, no clock read,
no environment -- and no import from this engine at all, including its own error
module. ``tests/test_architecture_boundaries.py`` enforces that list rather than
trusting it. Every function here is a total function of its arguments and the
calendar it is called on, which is why it is testable with plain values and no
fakes at all.

That import ban is why :class:`CalendarError` is defined here on top of
``ValueError`` instead of subclassing the engine's ``ConfigError``. It keeps the
house convention -- a message plus an actionable ``hint``, both on the
exception -- so the tier that consults this module can translate one into the
other in a line. That tier is the one allowed to know this engine exists; this
file is not.

**Two decisions about daylight saving, made deliberately.**

*Ambiguous* local times -- the hour that repeats when the clock falls back -- are
resolved to the **first** occurrence (``fold=0``). A market opens the first time
the clock reads its opening time; it does not decline to open and then open an
hour later. Taking the second occurrence would leave a scheduler asleep for an
hour while the session it is waiting for is already trading, which is the more
expensive of the two errors and the harder one to notice.

*Nonexistent* local times -- the hour that never happens when the clock springs
forward -- are **refused**, with :class:`CalendarError`. Python's default
arithmetic quietly maps ``02:30`` to ``03:30`` on such a day, inventing an
instant an hour past the one that was asked for. Both possible repairs (skip
forward to the transition, or fall back before it) change the declared length of
the session, in opposite directions, and choosing between them is a policy
decision about that market -- exactly the kind of decision this module does not
make. A caller whose session genuinely straddles a spring-forward gap has to say
what it wants for that date.

**Resolved instants come back in UTC**, not in the market's own zone, and that
is a correctness requirement rather than a preference. Python compares and
subtracts two aware datetimes that share a ``tzinfo`` *as if they were naive* --
the common offset is ignored. A :class:`Session` holding its edges in
``America/New_York`` would therefore report a fall-back session as three and a
half hours when it is four and a half, and would answer ``contains`` wrongly for
an instant handed to it in that same zone during the repeated hour. Both errors
are invisible in every month that has no transition. Storing UTC makes every
comparison in this module an instant comparison; a caller wanting the clock face
back calls ``.astimezone(calendar.timezone)`` and gets it exactly.

**Aware datetimes only.** A naive datetime is refused at every entry point
rather than assumed to be UTC or assumed to be local. Guessing would shift an
instant by hours in whichever direction happened to be convenient, and the
resulting "market is open" would be wrong for exactly as long as the guess was.

**One session per local date.** :class:`SessionHours` refuses ``opens_at >=
closes_at``, so a session lies entirely inside its own local date. A market with
an intraday break, or one whose session crosses local midnight, is not modelled
here -- deliberately, because :attr:`MarketCalendar.early_closes` names a single
time per date and could not say which of several sessions it truncates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

__all__ = [
    "MAX_SESSION_SEARCH_DAYS",
    "CalendarError",
    "DayKind",
    "BoundaryKind",
    "SessionHours",
    "Session",
    "SessionBoundary",
    "MarketCalendar",
]

# How far :meth:`MarketCalendar.next_session` will scan before giving up.
#
# A search bound, not a market rule: nothing here knows how long a year is. A
# calendar that declares no trading day within this many days of an instant has
# a holiday list or a weekend definition that is wrong, and an unbounded scan
# would express that as a hang -- the one failure mode a scheduler cannot report.
MAX_SESSION_SEARCH_DAYS = 366

# date.weekday() is Monday==0 .. Sunday==6. Named so the validator's message can
# say what the numbers mean without the reader going to look it up.
_MIN_WEEKDAY = 0
_MAX_WEEKDAY = 6


class CalendarError(ValueError):
    """The calendar cannot answer, because its data or its argument is wrong.

    Carries a ``hint`` alongside the message, which is where "you asked for 02:30
    on a spring-forward date" goes -- the part that turns a traceback into a fix.
    Same shape as the engine's own error base, deliberately, so the tier that
    consults this calendar can re-raise one as a configuration error without
    reformatting anything. It does not subclass that base because this module is
    forbidden from importing it; see the module docstring.

    ``ValueError`` rather than a bare ``Exception`` so that a caller who has not
    heard of this module still catches it under the category it belongs to: every
    refusal here is a value that was wrong.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message}\n  hint: {self.hint}" if self.hint else self.message


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise CalendarError(message, hint=hint)


def _require_aware(value: object, label: str) -> datetime:
    """The instant, or a refusal. Never a datetime whose zone is unknown.

    Prevents the failure where a naive datetime is assumed to be UTC by one
    caller and local by another, so ``is_open`` disagrees with itself by the size
    of the offset -- five hours, in the zone this engine mostly runs against, and
    silently only during the hours that matter.

    The ``isinstance`` check comes first on purpose: reading ``.tzinfo`` off a
    string raises ``AttributeError``, which escapes the ``CalendarError``
    contract a caller catches.
    """
    if not isinstance(value, datetime):
        _refuse(f"{label} must be a datetime, got {type(value).__name__}")
    instant: datetime = value  # type: ignore[assignment]
    if instant.tzinfo is None or instant.utcoffset() is None:
        _refuse(
            f"{label} must be timezone-aware, got the naive {instant.isoformat()}",
            hint="a naive timestamp has no instant attached to it; attach the "
            "zone it was read in rather than letting this module pick one",
        )
    return instant


def _require_plain_date(value: object, label: str) -> date:
    """A calendar date, and specifically not a datetime.

    ``datetime`` is a subclass of ``date``, so an ``isinstance(x, date)`` check
    written the obvious way accepts an instant and then uses ``.year/.month/.day``
    straight off it -- without ever converting to the calendar's zone. A UTC
    instant at ``2026-01-02T02:00Z`` is still ``2026-01-01`` in New York, so that
    silently asks about the wrong day, and does so only for instants near
    midnight. Refused here; :meth:`MarketCalendar.local_date` is the conversion.
    """
    if isinstance(value, datetime):
        _refuse(
            f"{label} must be a date, got a datetime ({value.isoformat()})",
            hint="a datetime's .date() is its date in whatever zone it carries, "
            "not in this calendar's zone; use local_date() or day_kind_at()",
        )
    if not isinstance(value, date):
        _refuse(f"{label} must be a date, got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _require_wall_time(value: object, label: str) -> time:
    """A wall-clock time of day, carrying no zone of its own.

    A tz-aware ``time`` is refused rather than honoured. Combined with a date it
    would override the calendar's zone for that one field, producing a session
    whose open and close are quoted in different zones -- which still yields two
    ordered instants, so nothing downstream would notice.
    """
    if not isinstance(value, time):
        _refuse(f"{label} must be a time, got {type(value).__name__}")
    wall: time = value  # type: ignore[assignment]
    if wall.tzinfo is not None:
        _refuse(
            f"{label} must be a bare wall-clock time, got one carrying "
            f"{wall.tzinfo!r}",
            hint="the zone belongs to the calendar, not to the hour; a per-field "
            "zone would let the open and the close be quoted in different ones",
        )
    return wall


class DayKind(Enum):
    """What a calendar date is, from the point of view of trading on it.

    Four cases rather than a bool because a scheduler that only knows "closed"
    cannot tell an operator why, and because ``EARLY_CLOSE`` is a trading day --
    collapsing it into ``TRADING`` would lose the only thing that makes it
    different, and collapsing it into a closed day would skip a session.
    """

    TRADING = "trading"
    EARLY_CLOSE = "early_close"
    HOLIDAY = "holiday"
    WEEKEND = "weekend"

    @property
    def has_session(self) -> bool:
        """Whether the market trades at all on a day of this kind."""
        return self in (DayKind.TRADING, DayKind.EARLY_CLOSE)


class BoundaryKind(Enum):
    """Which way the market is about to move at a boundary.

    A scheduler that sleeps until a boundary needs to know what it will wake up
    to; a bare instant would leave it to re-derive that, and to get it wrong at
    exactly the instant the two answers meet.
    """

    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class SessionHours:
    """The wall-clock open and close of one trading day. No zone, no date.

    Both fields are required. There is deliberately no default pair of hours:
    a default would be correct for one venue and quietly wrong for every other,
    and "quietly wrong" here means the engine believes it is trading a live
    session while the book is closed.

    ``opens_at >= closes_at`` is refused, which is also what pins a session
    inside a single local date -- every lookup in this module relies on that.
    """

    opens_at: time
    closes_at: time

    def __post_init__(self) -> None:
        opens = _require_wall_time(self.opens_at, "opens_at")
        closes = _require_wall_time(self.closes_at, "closes_at")
        if opens >= closes:
            _refuse(
                f"opens_at {opens.isoformat()} is not before closes_at "
                f"{closes.isoformat()}",
                hint="a session that ends when or before it begins is never open; "
                "a session crossing local midnight is not modelled here",
            )


@dataclass(frozen=True)
class Session:
    """One resolved trading session, as two instants rather than two clock faces.

    The whole point of this type is that ``opens_at`` and ``closes_at`` are
    aware datetimes, already through the zone conversion. A caller comparing an
    instant against a wall-clock ``time`` would be right for most of the year and
    an hour wrong twice, which is the bug this module exists to remove.

    Both are in UTC; ``day`` is the local date they belong to. That split is
    deliberate. Two aware datetimes sharing a ``tzinfo`` are compared and
    subtracted by Python as if they were naive, so edges stored in the market's
    own zone would make :attr:`duration` the difference of two clock faces and
    :meth:`contains` wrong for a same-zone instant inside a repeated hour. UTC
    has no transitions, so neither shortcut can misfire. Call
    ``.astimezone(calendar.timezone)`` for the wall-clock reading.

    The interval is half-open, ``[opens_at, closes_at)``. At the closing instant
    the market is closed: an inclusive close would make :meth:`contains` answer
    "open" at the exact instant :meth:`MarketCalendar.next_boundary` had told the
    scheduler the session ends, and a scheduler that wakes to a contradiction
    between two of its own calls tends to spin.
    """

    day: date
    kind: DayKind
    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        _require_plain_date(self.day, "day")
        if not isinstance(self.kind, DayKind):
            _refuse(f"kind must be a DayKind, got {type(self.kind).__name__}")
        if not self.kind.has_session:
            _refuse(
                f"a Session cannot describe a {self.kind.value} day",
                hint="days without a session are represented by None, not by a "
                "zero-length Session nobody thinks to check",
            )
        opens = _require_aware(self.opens_at, "opens_at").astimezone(UTC)
        closes = _require_aware(self.closes_at, "closes_at").astimezone(UTC)
        # Normalised here, in the value type, rather than trusted from the
        # factory. Everything this class promises about duration and containment
        # depends on the edges having no transitions in their own zone, and a
        # caller can construct a Session directly: given edges in the market's
        # zone, a fall-back session reports four hours instead of five and
        # `contains` cannot tell the two passes of the repeated hour apart. An
        # invariant stated only in a docstring is a hypothesis; converting is
        # what makes it true.
        object.__setattr__(self, "opens_at", opens)
        object.__setattr__(self, "closes_at", closes)
        if closes <= opens:
            _refuse(
                f"closes_at {closes.isoformat()} is not after opens_at "
                f"{opens.isoformat()}"
            )

    @property
    def duration(self) -> timedelta:
        """How long the market is actually open. Not a wall-clock subtraction.

        On a daylight-saving date this differs from the difference of the two
        clock faces by the size of the transition, which is the number a caller
        sizing a time-based schedule actually needs.
        """
        return self.closes_at - self.opens_at

    def contains(self, instant: datetime) -> bool:
        """Whether ``instant`` falls in ``[opens_at, closes_at)``."""
        moment = _require_aware(instant, "instant")
        return self.opens_at <= moment < self.closes_at


@dataclass(frozen=True)
class SessionBoundary:
    """The next instant the market's open/closed state changes, and which way.

    Carries the session it belongs to so a scheduler that woke up can act without
    a second lookup against a calendar that may have been rebuilt in between --
    two lookups that disagree is how a scheduler ends up acting on a session it
    is not actually in.

    ``at`` is in UTC, for the same reason :class:`Session`'s edges are and
    enforced the same way: :meth:`time_from` subtracts it from an instant the
    caller supplies, and Python subtracts two aware datetimes sharing a
    ``tzinfo`` as if they were naive. Call ``.astimezone(calendar.timezone)``
    for the wall-clock reading.
    """

    at: datetime
    kind: BoundaryKind
    session: Session

    def __post_init__(self) -> None:
        at = _require_aware(self.at, "at").astimezone(UTC)
        # Normalised here, in the value type, exactly as Session normalises its
        # edges and against the same failure. A boundary constructed directly
        # with a market-zone datetime makes `time_from` a subtraction of two
        # clock faces whenever the caller's instant carries that same zone: an
        # hour out across a transition, and identical for both passes of a
        # repeated hour, which is a scheduler handed a sleep to the wrong
        # instant -- or a negative one. The edge check below cannot stand in for
        # this, because comparing a market-zone datetime against the session's
        # UTC edge is already an instant comparison, so it passes and leaves the
        # un-normalised value in place.
        object.__setattr__(self, "at", at)
        if not isinstance(self.kind, BoundaryKind):
            _refuse(f"kind must be a BoundaryKind, got {type(self.kind).__name__}")
        if not isinstance(self.session, Session):
            _refuse(f"session must be a Session, got {type(self.session).__name__}")
        expected = (
            self.session.opens_at
            if self.kind is BoundaryKind.OPEN
            else self.session.closes_at
        )
        if self.at != expected:
            _refuse(
                f"boundary at {self.at.isoformat()} is not the session's "
                f"{self.kind.value} ({expected.isoformat()})",
                hint="a boundary that does not sit on its own session's edge "
                "would send a scheduler to sleep until the wrong instant",
            )

    def time_from(self, instant: datetime) -> timedelta:
        """How long from ``instant`` until this boundary. A sleep duration.

        May be negative or zero if the caller passes an instant at or after the
        boundary; that is left visible rather than clamped, because a negative
        sleep is a bug in the caller's loop and silently clamping it to zero
        turns that bug into a busy spin nobody can find.
        """
        return self.at - _require_aware(instant, "instant")


@dataclass(frozen=True)
class MarketCalendar:
    """Which days trade, at what hours, in which zone -- all of it supplied.

    Every field is required. There is no ``MarketCalendar()``: a caller cannot
    obtain a session definition without having stated one, which is the whole
    reason this type exists rather than a module-level pair of constants. The
    failure that prevents is a scheduler that runs against plausible-looking
    defaults nobody chose, and reports success the entire time.

    Frozen, and built from tuples rather than sets or dicts, so an instance is
    hashable and cannot be edited after a scheduling decision was made against
    it -- the calendar that said "open" is the calendar that gets recorded.

    ``weekend_days`` is data too, not a constant. Saturday and Sunday is a fact
    about most Western venues and not about all of them, and a calendar that
    hardcoded it would answer confidently and wrongly for the ones where it is
    Friday and Saturday.
    """

    #: Must be a :class:`~zoneinfo.ZoneInfo`. A fixed-offset ``tzinfo`` is
    #: refused: it cannot express a daylight-saving transition, so a calendar
    #: built on one is correct in January and an hour wrong in July.
    timezone: ZoneInfo
    #: The regular open and close, in this calendar's local wall-clock time.
    regular_hours: SessionHours
    #: ``date.weekday()`` values that never trade. Monday is 0, Sunday is 6.
    weekend_days: tuple[int, ...]
    #: Dates with no session at all.
    holidays: tuple[date, ...]
    #: ``(date, closing wall-clock time)`` for days that end early.
    early_closes: tuple[tuple[date, time], ...]

    # -- validation --------------------------------------------------------

    def __post_init__(self) -> None:
        if not isinstance(self.timezone, ZoneInfo):
            _refuse(
                f"timezone must be a zoneinfo.ZoneInfo, got "
                f"{type(self.timezone).__name__}",
                hint="a fixed-offset tzinfo cannot express a daylight-saving "
                "transition; pass ZoneInfo('UTC') if the session really is "
                "defined in UTC",
            )
        if not isinstance(self.regular_hours, SessionHours):
            _refuse(
                f"regular_hours must be a SessionHours, got "
                f"{type(self.regular_hours).__name__}"
            )

        self._check_weekend_days()
        self._check_holidays()
        self._check_early_closes()

    def _check_weekend_days(self) -> None:
        """Refuse a weekend definition that cannot describe a market.

        All seven days is the interesting one: it makes every lookup answer
        "closed" forever, so :meth:`next_session` would scan its whole bound and
        raise -- a year of holidays reported as a data problem, at whatever hour
        the scheduler first happened to need a session.
        """
        if not isinstance(self.weekend_days, tuple):
            _refuse(
                f"weekend_days must be a tuple, got "
                f"{type(self.weekend_days).__name__}",
                hint="a mutable collection would break the frozen calendar's "
                "hashability along with its immutability",
            )
        seen: set[int] = set()
        for day in self.weekend_days:
            # bool is a subclass of int, so True would otherwise be accepted and
            # silently mean Tuesday.
            if not isinstance(day, int) or isinstance(day, bool):
                _refuse(
                    f"weekend_days entries must be ints, got {day!r}",
                    hint="date.weekday() values: Monday is 0, Sunday is 6",
                )
            if not _MIN_WEEKDAY <= day <= _MAX_WEEKDAY:
                _refuse(
                    f"weekend_days entry {day} is not a weekday number",
                    hint=f"date.weekday() returns {_MIN_WEEKDAY}..{_MAX_WEEKDAY}, "
                    "Monday through Sunday",
                )
            if day in seen:
                _refuse(f"weekend_days names day {day} more than once")
            seen.add(day)
        if len(seen) > _MAX_WEEKDAY:
            _refuse(
                "weekend_days names every day of the week",
                hint="a calendar with no trading day at all can only ever answer "
                "'closed'; to stop trading, use the HALT file",
            )

    def _check_holidays(self) -> None:
        if not isinstance(self.holidays, tuple):
            _refuse(f"holidays must be a tuple, got {type(self.holidays).__name__}")
        seen: set[date] = set()
        for day in self.holidays:
            _require_plain_date(day, "holidays entry")
            if day in seen:
                _refuse(f"holidays names {day.isoformat()} more than once")
            seen.add(day)

    def _check_early_closes(self) -> None:
        """Refuse an early close that contradicts the rest of the calendar.

        Each refusal here is a way the entry would have had no effect while
        looking like a configured one: a second time for the same date (only one
        of them can win), a date that is already closed (nothing to shorten), and
        a time at or after the regular close (not early, and at or before the
        open it is a holiday written in the wrong field).
        """
        if not isinstance(self.early_closes, tuple):
            _refuse(
                f"early_closes must be a tuple of (date, time) pairs, got "
                f"{type(self.early_closes).__name__}"
            )
        seen: set[date] = set()
        for entry in self.early_closes:
            if not isinstance(entry, tuple) or len(entry) != 2:
                _refuse(
                    f"early_closes entries must be (date, time) pairs, got {entry!r}"
                )
            raw_day, raw_close = entry
            day = _require_plain_date(raw_day, "early_closes date")
            closes_at = _require_wall_time(raw_close, "early_closes time")

            if day in seen:
                _refuse(
                    f"early_closes names {day.isoformat()} more than once",
                    hint="two closing times for one date makes the session depend "
                    "on which entry is read first",
                )
            seen.add(day)

            if day.weekday() in self.weekend_days:
                _refuse(
                    f"early_closes names {day.isoformat()}, which is a weekend day",
                    hint="there is no session on that date to shorten",
                )
            if day in self.holidays:
                _refuse(
                    f"early_closes names {day.isoformat()}, which is also a holiday",
                    hint="a holiday has no session to close early; drop one of the "
                    "two entries so the calendar states one thing",
                )
            if closes_at >= self.regular_hours.closes_at:
                _refuse(
                    f"early close {closes_at.isoformat()} on {day.isoformat()} is "
                    f"not before the regular close "
                    f"{self.regular_hours.closes_at.isoformat()}",
                    hint="an early close that is not earlier changes nothing while "
                    "reading in a report as a short day",
                )
            if closes_at <= self.regular_hours.opens_at:
                _refuse(
                    f"early close {closes_at.isoformat()} on {day.isoformat()} is "
                    f"not after the open "
                    f"{self.regular_hours.opens_at.isoformat()}",
                    hint="a session that closes before it opens is a holiday; say "
                    "so in holidays rather than here",
                )

    # -- zone arithmetic ---------------------------------------------------

    def local_date(self, instant: datetime) -> date:
        """The calendar date ``instant`` falls on, in this calendar's zone.

        Not ``instant.date()``. That would be the date in whatever zone the
        caller's datetime happens to carry, and ``2026-01-02T02:00Z`` is still
        ``2026-01-01`` in New York -- so a UTC-stamped instant would be attributed
        to tomorrow's session for the five hours around midnight.
        """
        return _require_aware(instant, "instant").astimezone(self.timezone).date()

    def _resolve(self, day: date, wall: time) -> datetime:
        """A local wall-clock time on a date, as the UTC instant it names.

        The two daylight-saving cases are the reason this is not a one-line
        ``datetime.combine``. See the module docstring for the decisions; in
        short, the repeated hour resolves to its first occurrence and the hour
        that never happens is refused rather than silently moved.

        The return is normalised to UTC before it leaves. Handing back a
        zone-local datetime would look friendlier and would make every later
        subtraction and comparison against another datetime in the same zone
        wall-clock arithmetic -- so a session spanning a transition would report
        the difference of two clock faces rather than the time that actually
        passed.
        """
        naive = datetime.combine(day, wall)
        first = naive.replace(tzinfo=self.timezone, fold=0)
        second = naive.replace(tzinfo=self.timezone, fold=1)
        first_offset = first.utcoffset()
        second_offset = second.utcoffset()

        if first_offset is None or second_offset is None:  # pragma: no cover
            # Unreachable for a ZoneInfo, which __post_init__ requires; kept so a
            # future relaxation of that check fails loudly instead of comparing
            # None and raising TypeError from inside a scheduler's sleep loop.
            _refuse(f"{self.timezone} gives no UTC offset for {naive.isoformat()}")

        if first_offset == second_offset:
            return first.astimezone(UTC)
        if first_offset > second_offset:  # type: ignore[operator]
            # The clock fell back, so this wall time happens twice. The market
            # opens the first time the clock reads it.
            return first.astimezone(UTC)
        # The clock sprang forward: this wall time never happens on this date.
        gap = second_offset - first_offset  # type: ignore[operator]
        _refuse(
            f"{wall.isoformat()} does not exist on {day.isoformat()} in "
            f"{self.timezone}: the clock skips {gap} that morning",
            hint="both repairs -- shifting the session forward past the gap or "
            "back before it -- change how long the market is declared open, and "
            "which one is right is a fact about that market, not about "
            "timezones; state the intended hours for this date",
        )
        raise AssertionError("unreachable")  # pragma: no cover

    # -- day classification ------------------------------------------------

    def day_kind(self, day: date) -> DayKind:
        """What kind of day this date is. Refuses a datetime, see below.

        Weekend is checked before holiday. A holiday entry that lands on a
        weekend day is redundant -- the venue was already closed -- and reporting
        ``WEEKEND`` keeps that visible instead of implying the market would
        otherwise have opened. Early close is checked last, so it can only ever
        describe a day that trades.

        Passing a datetime is refused rather than accepted: ``datetime`` is a
        subclass of ``date``, so the obvious check lets one through and then
        reads its date in the caller's zone rather than this calendar's. Use
        :meth:`day_kind_at`.
        """
        target = _require_plain_date(day, "day")
        if target.weekday() in self.weekend_days:
            return DayKind.WEEKEND
        if target in self.holidays:
            return DayKind.HOLIDAY
        for early_day, _ in self.early_closes:
            if early_day == target:
                return DayKind.EARLY_CLOSE
        return DayKind.TRADING

    def day_kind_at(self, instant: datetime) -> DayKind:
        """What kind of day ``instant`` falls on, in this calendar's zone."""
        return self.day_kind(self.local_date(instant))

    def closing_time_on(self, day: date) -> time | None:
        """The wall-clock close for this date, or ``None`` if it does not trade.

        Separate from :meth:`session_on` because it is the one question that can
        be answered without the zone arithmetic, and therefore without the
        daylight-saving refusal -- a report listing short days should not be able
        to fail on a date whose hours nobody has resolved yet.
        """
        target = _require_plain_date(day, "day")
        if not self.day_kind(target).has_session:
            return None
        for early_day, closes_at in self.early_closes:
            if early_day == target:
                return closes_at
        return self.regular_hours.closes_at

    # -- session resolution ------------------------------------------------

    def session_on(self, day: date) -> Session | None:
        """The session on this date as two instants, or ``None`` if it is closed.

        ``None`` rather than a zero-length session, so a caller that forgets to
        check gets an ``AttributeError`` at the point of the mistake instead of a
        session that is open for no time at all and reads as a quiet market.
        """
        target = _require_plain_date(day, "day")
        kind = self.day_kind(target)
        if not kind.has_session:
            return None

        closes_wall = self.regular_hours.closes_at
        for early_day, early_time in self.early_closes:
            if early_day == target:
                closes_wall = early_time
                break

        opens_at = self._resolve(target, self.regular_hours.opens_at)
        closes_at = self._resolve(target, closes_wall)
        if closes_at <= opens_at:
            # Defensive, and not reachable through any one-hour transition: a
            # session straddling a spring-forward gap must span more than the
            # gap in wall-clock terms, since the gap's own start and end are an
            # hour apart on the clock. It is kept for a zone whose transition is
            # larger than the session, where the wall clock would still read
            # open-before-close and Session's own check would refuse with a
            # message naming neither the date nor the transition.
            _refuse(
                f"on {target.isoformat()} the session collapses: "
                f"{self.regular_hours.opens_at.isoformat()} resolves to "
                f"{opens_at.isoformat()} and {closes_wall.isoformat()} resolves "
                f"to {closes_at.isoformat()}",
                hint="a daylight-saving transition inside these hours consumed "
                "the whole session; state the intended hours for this date",
            )
        return Session(
            day=target, kind=kind, opens_at=opens_at, closes_at=closes_at
        )

    def current_session(self, instant: datetime) -> Session | None:
        """The session containing ``instant``, or ``None`` if none does.

        Only the instant's own local date is examined, which is sound precisely
        because :class:`SessionHours` refuses ``opens_at >= closes_at``: a
        session cannot start on one local date and end on another.
        """
        moment = _require_aware(instant, "instant")
        session = self.session_on(self.local_date(moment))
        if session is not None and session.contains(moment):
            return session
        return None

    def is_open(self, instant: datetime) -> bool:
        """Whether the market is trading at ``instant``. Half-open, see Session."""
        return self.current_session(instant) is not None

    def next_session(self, instant: datetime) -> Session:
        """The next session that opens strictly after ``instant``.

        Strictly after, and therefore never the session ``instant`` is inside --
        :meth:`current_session` answers that one. Two methods rather than one
        because a scheduler mid-session asking "when is the next open" wants
        tomorrow, and a scheduler at midnight asking the same wants today, and a
        single method would have to be wrong for one of them.

        Raises rather than returning ``None`` when nothing is found within
        :data:`MAX_SESSION_SEARCH_DAYS`. A ``None`` would be indistinguishable
        from "closed right now" at the call site and would be handled as such,
        which turns a broken holiday list into a scheduler that simply never runs.
        """
        moment = _require_aware(instant, "instant")
        first_day = self.local_date(moment)
        for offset in range(MAX_SESSION_SEARCH_DAYS + 1):
            session = self.session_on(first_day + timedelta(days=offset))
            if session is not None and session.opens_at > moment:
                return session
        _refuse(
            f"no session opens within {MAX_SESSION_SEARCH_DAYS} days of "
            f"{moment.isoformat()}",
            hint="the holiday list or the weekend definition closes the market "
            "for longer than a year; a scheduler cannot wait that out",
        )
        raise AssertionError("unreachable")  # pragma: no cover

    def next_boundary(self, instant: datetime) -> SessionBoundary:
        """The next instant the open/closed answer changes, and which way.

        This is the method a scheduler sizes a sleep from. Inside a session the
        boundary is that session's close; outside one it is the next session's
        open. The two cases meet exactly at the closing instant, and the
        half-open interval is what keeps them from both claiming it.
        """
        moment = _require_aware(instant, "instant")
        current = self.current_session(moment)
        if current is not None:
            return SessionBoundary(
                at=current.closes_at, kind=BoundaryKind.CLOSE, session=current
            )
        upcoming = self.next_session(moment)
        return SessionBoundary(
            at=upcoming.opens_at, kind=BoundaryKind.OPEN, session=upcoming
        )

    def time_until_next_boundary(self, instant: datetime) -> timedelta:
        """How long a scheduler may sleep before the answer changes.

        Always strictly positive: the close of a session containing ``instant``
        is after it by the half-open rule, and the open of the next session is
        after it by construction. A scheduler can pass this straight to a sleep
        without a guard against zero, which is the guard that gets forgotten.
        """
        return self.next_boundary(instant).time_from(instant)
