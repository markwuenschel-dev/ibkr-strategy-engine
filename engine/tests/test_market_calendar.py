"""Trading-session arithmetic: every way a scheduler could be told the wrong hour.

Two failures dominate this file. The first is a timezone answer that is right
for most of the year and an hour wrong twice, which presents as an engine that
missed a session or traded past a close and leaves no evidence of why. The
second is a policy default sneaking in -- a hardcoded 09:30, a shipped holiday
list -- which makes the calendar answer confidently for a market nobody
configured.

Every date here is real. The daylight-saving cases use the actual 2026
America/New_York transitions (spring forward Sunday 8 March, fall back Sunday
1 November), because a synthetic zone cannot prove the arithmetic against the
tz database the engine will actually run on.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from engine.errors import ConfigError
from engine.market_calendar import (
    MAX_SESSION_SEARCH_DAYS,
    BoundaryKind,
    CalendarError,
    DayKind,
    MarketCalendar,
    Session,
    SessionBoundary,
    SessionHours,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")

# -- test data, not shipped policy -------------------------------------------
#
# These live in the test file precisely because the module refuses to ship them.
# A reader who wants to know what hours the engine trades will not find the
# answer by importing engine.market_calendar, which is the point.

REGULAR = SessionHours(opens_at=time(9, 30), closes_at=time(16, 0))
SATURDAY_AND_SUNDAY = (5, 6)

NEW_YEAR = date(2026, 1, 1)  # Thursday
INDEPENDENCE_DAY = date(2026, 7, 4)  # a Saturday in 2026
THANKSGIVING = date(2026, 11, 26)  # Thursday
CHRISTMAS = date(2026, 12, 25)  # Friday

HALF_DAY = date(2026, 11, 27)  # Friday after Thanksgiving
HALF_DAY_CLOSE = time(13, 0)

# Ordinary weekdays either side of the 2026 transitions.
FRIDAY_EST_MARCH = date(2026, 3, 6)
MONDAY_EDT_MARCH = date(2026, 3, 9)
FRIDAY_EDT_OCTOBER = date(2026, 10, 30)
MONDAY_EST_NOVEMBER = date(2026, 11, 2)

SPRING_FORWARD = date(2026, 3, 8)  # 02:00 -> 03:00, Sunday
FALL_BACK = date(2026, 11, 1)  # 02:00 -> 01:00, Sunday

MONDAY = date(2026, 11, 30)
TUESDAY = date(2026, 12, 1)


def calendar_with(**overrides: object) -> MarketCalendar:
    """A complete calendar with one or more fields replaced.

    Spelled out rather than defaulted inside the module: the helper exists so
    each test can vary one field, not so a calendar can be had for free.
    """
    values: dict[str, object] = {
        "timezone": NY,
        "regular_hours": REGULAR,
        "weekend_days": SATURDAY_AND_SUNDAY,
        "holidays": (NEW_YEAR, INDEPENDENCE_DAY, THANKSGIVING, CHRISTMAS),
        "early_closes": ((HALF_DAY, HALF_DAY_CLOSE),),
    }
    values.update(overrides)
    return MarketCalendar(**values)  # type: ignore[arg-type]


def night_calendar(opens: time, closes: time) -> MarketCalendar:
    """A calendar whose session straddles the small hours, weekends included.

    The daylight-saving transitions happen at 02:00 on a Sunday, so a 09:30
    session never touches them. Reaching the ambiguous and nonexistent cases at
    all requires a session that does -- which is also a reminder that the module
    must not assume the transition falls outside trading hours.
    """
    return MarketCalendar(
        timezone=NY,
        regular_hours=SessionHours(opens_at=opens, closes_at=closes),
        weekend_days=(),
        holidays=(),
        early_closes=(),
    )


# ===========================================================================
# No policy is baked in
# ===========================================================================


class TestNoPolicyIsBaked:
    """The module must not be able to answer for a market nobody configured.

    A default pair of hours or a shipped holiday list would be right for one
    venue and silently wrong for every other, and the wrongness surfaces as a
    scheduler that slept through a trading day -- which nobody traces back to a
    constant in a calendar module.
    """

    def test_a_calendar_cannot_be_constructed_without_data(self) -> None:
        with pytest.raises(TypeError):
            MarketCalendar()  # type: ignore[call-arg]

    def test_every_calendar_field_is_required(self) -> None:
        """Omitting any one of them must fail, not fall back to a guess."""
        complete: dict[str, object] = {
            "timezone": NY,
            "regular_hours": REGULAR,
            "weekend_days": SATURDAY_AND_SUNDAY,
            "holidays": (),
            "early_closes": (),
        }
        for omitted in complete:
            partial = {k: v for k, v in complete.items() if k != omitted}
            with pytest.raises(TypeError):
                MarketCalendar(**partial)  # type: ignore[arg-type]

    def test_session_hours_cannot_be_constructed_without_data(self) -> None:
        with pytest.raises(TypeError):
            SessionHours()  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            SessionHours(opens_at=time(9, 30))  # type: ignore[call-arg]

    def test_the_module_ships_no_hours_dates_or_prebuilt_calendar(self) -> None:
        """The enforcement, not just the intent: no module-level value is a
        session definition, a date, a time, or an assembled calendar."""
        import engine.market_calendar as module

        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            assert not isinstance(
                value, (SessionHours, MarketCalendar, Session, date, time, timedelta)
            ), name

    def test_the_only_module_constant_is_a_search_bound(self) -> None:
        """366 is how far next_session scans before reporting a broken holiday
        list. It is not a statement about any market's year."""
        assert MAX_SESSION_SEARCH_DAYS == 366
        assert isinstance(MAX_SESSION_SEARCH_DAYS, int)


# ===========================================================================
# SessionHours
# ===========================================================================


class TestSessionHoursValidation:
    """A pair of hours that survives construction badly is a session that is
    never open, or one that quietly spans midnight and breaks every lookup."""

    def test_ordinary_hours_are_kept_exactly(self) -> None:
        hours = SessionHours(opens_at=time(9, 30), closes_at=time(16, 0))
        assert hours.opens_at == time(9, 30)
        assert hours.closes_at == time(16, 0)

    def test_equal_open_and_close_refused(self) -> None:
        """A session that ends when it begins is never open, and reads in a
        report as a market that offered nothing."""
        with pytest.raises(CalendarError) as exc:
            SessionHours(opens_at=time(9, 30), closes_at=time(9, 30))
        assert "not before" in str(exc.value)

    def test_close_before_open_refused(self) -> None:
        """This is how a session crossing local midnight would be spelled, and
        every lookup here assumes a session lies inside one local date."""
        with pytest.raises(CalendarError) as exc:
            SessionHours(opens_at=time(20, 0), closes_at=time(4, 0))
        assert "midnight" in str(exc.value)

    def test_a_zone_carrying_time_refused(self) -> None:
        """A per-field zone would let the open and the close be quoted in
        different ones, still yielding two ordered instants that nothing
        downstream would question."""
        with pytest.raises(CalendarError) as exc:
            SessionHours(opens_at=time(9, 30, tzinfo=NY), closes_at=time(16, 0))
        assert "wall-clock" in str(exc.value)

    def test_a_datetime_instead_of_a_time_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            SessionHours(
                opens_at=datetime(2026, 3, 6, 9, 30),  # type: ignore[arg-type]
                closes_at=time(16, 0),
            )
        assert "must be a time" in str(exc.value)

    def test_a_string_instead_of_a_time_refused(self) -> None:
        with pytest.raises(CalendarError):
            SessionHours(opens_at="09:30", closes_at=time(16, 0))  # type: ignore[arg-type]

    def test_hours_are_frozen_and_hashable(self) -> None:
        hours = SessionHours(opens_at=time(9, 30), closes_at=time(16, 0))
        with pytest.raises(Exception):
            hours.opens_at = time(10, 0)  # type: ignore[misc]
        assert isinstance(hash(hours), int)


# ===========================================================================
# Calendar construction
# ===========================================================================


class TestCalendarValidation:
    """Each refusal here is data that would have looked configured and done
    nothing, or done something nobody wrote down."""

    def test_a_complete_calendar_constructs(self) -> None:
        assert calendar_with().timezone is NY

    def test_a_fixed_offset_timezone_refused(self) -> None:
        """timezone.utc cannot express a transition, so a calendar built on one
        is correct in January and an hour wrong in July."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(timezone=UTC)
        assert "ZoneInfo" in str(exc.value)

    def test_a_timezone_name_as_a_string_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(timezone="America/New_York")
        assert "ZoneInfo" in str(exc.value)

    def test_zoneinfo_utc_is_accepted_for_a_market_defined_in_utc(self) -> None:
        """Refusing fixed offsets must not lock out a genuinely UTC venue."""
        assert calendar_with(timezone=ZoneInfo("UTC")).timezone == ZoneInfo("UTC")

    def test_regular_hours_must_be_session_hours(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(regular_hours=(time(9, 30), time(16, 0)))
        assert "SessionHours" in str(exc.value)

    def test_a_list_of_weekend_days_refused(self) -> None:
        """A mutable collection breaks the frozen calendar's hashability along
        with its immutability."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(weekend_days=[5, 6])
        assert "tuple" in str(exc.value)

    def test_a_weekday_number_out_of_range_refused(self) -> None:
        for value in (-1, 7, 99):
            with pytest.raises(CalendarError) as exc:
                calendar_with(weekend_days=(value,))
            assert "weekday number" in str(exc.value), value

    def test_a_bool_weekend_day_refused(self) -> None:
        """bool is a subclass of int, so True would be accepted by the obvious
        check and silently mean Tuesday."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(weekend_days=(True,))
        assert "ints" in str(exc.value)

    def test_a_duplicated_weekend_day_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(weekend_days=(5, 5))
        assert "more than once" in str(exc.value)

    def test_a_seven_day_weekend_refused(self) -> None:
        """Every lookup would answer 'closed' forever, and next_session would
        scan its whole bound before reporting it -- at whatever hour the
        scheduler first needed a session."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(weekend_days=(0, 1, 2, 3, 4, 5, 6))
        assert "every day of the week" in str(exc.value)

    def test_an_empty_weekend_is_accepted(self) -> None:
        """A venue that trades every day is unusual, not malformed; refusing it
        would be this module inventing a market."""
        assert calendar_with(weekend_days=(), early_closes=()).weekend_days == ()

    def test_a_list_of_holidays_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(holidays=[CHRISTMAS])
        assert "tuple" in str(exc.value)

    def test_a_datetime_in_the_holiday_list_refused(self) -> None:
        """datetime is a subclass of date, so it would be stored and then never
        compare equal to any date the lookup produces -- a holiday silently
        demoted to an ordinary trading day."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(holidays=(datetime(2026, 12, 25, tzinfo=UTC),))
        assert "got a datetime" in str(exc.value)

    def test_a_duplicated_holiday_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(holidays=(CHRISTMAS, CHRISTMAS))
        assert "more than once" in str(exc.value)

    def test_an_empty_holiday_list_is_accepted(self) -> None:
        """Empty means the operator has declared none, which is a statement.
        Refusing it would force a placeholder date that does trade."""
        assert calendar_with(holidays=(), early_closes=()).holidays == ()

    def test_a_non_pair_early_close_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(early_closes=((HALF_DAY,),))
        assert "(date, time) pairs" in str(exc.value)

    def test_a_duplicated_early_close_date_refused(self) -> None:
        """Two closing times for one date makes the session depend on which
        entry happens to be read first."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(
                early_closes=((HALF_DAY, time(13, 0)), (HALF_DAY, time(14, 0)))
            )
        assert "more than once" in str(exc.value)

    def test_an_early_close_on_a_weekend_day_refused(self) -> None:
        """There is no session on that date to shorten, so the entry would look
        configured and do nothing."""
        saturday = date(2026, 11, 28)
        with pytest.raises(CalendarError) as exc:
            calendar_with(early_closes=((saturday, time(13, 0)),))
        assert "weekend day" in str(exc.value)

    def test_an_early_close_on_a_holiday_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            calendar_with(early_closes=((CHRISTMAS, time(13, 0)),))
        assert "also a holiday" in str(exc.value)

    def test_an_early_close_at_the_regular_close_refused(self) -> None:
        """Not earlier, so it changes nothing while reading in a report as a
        short day."""
        with pytest.raises(CalendarError) as exc:
            calendar_with(early_closes=((HALF_DAY, time(16, 0)),))
        assert "not before the regular close" in str(exc.value)

    def test_an_early_close_after_the_regular_close_refused(self) -> None:
        with pytest.raises(CalendarError):
            calendar_with(early_closes=((HALF_DAY, time(17, 0)),))

    def test_an_early_close_at_or_before_the_open_refused(self) -> None:
        """A session that closes before it opens is a holiday, and belongs in
        the holiday list where a reader will look for it."""
        for value in (time(9, 30), time(8, 0)):
            with pytest.raises(CalendarError) as exc:
                calendar_with(early_closes=((HALF_DAY, value),))
            assert "not after the open" in str(exc.value), value

    def test_a_zone_carrying_early_close_time_refused(self) -> None:
        with pytest.raises(CalendarError):
            calendar_with(early_closes=((HALF_DAY, time(13, 0, tzinfo=NY)),))

    def test_calendar_error_is_a_value_error_carrying_a_hint(self) -> None:
        """The module may not import the engine's error base, so it reproduces
        its shape: a caller that has not heard of this module still catches the
        refusal as a ValueError, and the tier that has can re-raise it as a
        configuration error without reformatting the message."""
        assert issubclass(CalendarError, ValueError)
        assert not issubclass(CalendarError, ConfigError)
        with pytest.raises(ValueError) as exc:
            calendar_with(timezone=UTC)
        assert exc.value.hint  # type: ignore[attr-defined]
        assert exc.value.message  # type: ignore[attr-defined]
        assert exc.value.hint in str(exc.value)  # type: ignore[attr-defined]

    def test_the_calendar_imports_nothing_from_this_engine(self) -> None:
        """Cheap local guard on the same rule tests/test_architecture_boundaries.py
        enforces by AST: a calendar that drags the engine in stops being the
        thing anything can afford to consult."""
        import engine.market_calendar as module

        for value in vars(module).values():
            origin = str(getattr(value, "__module__", ""))
            if origin == module.__name__:
                continue  # the module's own types
            assert not origin.startswith("engine"), value


# ===========================================================================
# Day classification
# ===========================================================================


class TestDayClassification:
    """A scheduler that only knows 'closed' cannot tell an operator why, and an
    early close collapsed into either bucket loses a session or a short day."""

    def test_an_ordinary_weekday_trades(self) -> None:
        assert calendar_with().day_kind(MONDAY) is DayKind.TRADING

    def test_saturday_and_sunday_are_the_weekend(self) -> None:
        assert calendar_with().day_kind(date(2026, 11, 28)) is DayKind.WEEKEND
        assert calendar_with().day_kind(date(2026, 11, 29)) is DayKind.WEEKEND

    def test_a_declared_holiday_has_no_session(self) -> None:
        assert calendar_with().day_kind(THANKSGIVING) is DayKind.HOLIDAY
        assert calendar_with().session_on(THANKSGIVING) is None

    def test_a_declared_early_close_is_still_a_trading_day(self) -> None:
        assert calendar_with().day_kind(HALF_DAY) is DayKind.EARLY_CLOSE
        assert calendar_with().session_on(HALF_DAY) is not None

    def test_a_holiday_falling_on_a_weekend_reports_the_weekend(self) -> None:
        """4 July 2026 is a Saturday. Reporting HOLIDAY would imply the market
        would otherwise have opened; the venue was already closed."""
        assert INDEPENDENCE_DAY.weekday() == 5
        assert calendar_with().day_kind(INDEPENDENCE_DAY) is DayKind.WEEKEND

    def test_the_weekend_definition_is_data_not_a_constant(self) -> None:
        """A venue trading Sunday through Thursday must classify correctly, or
        the module has quietly hardcoded a Western week."""
        gulf = calendar_with(weekend_days=(4, 5), early_closes=())
        assert gulf.day_kind(date(2026, 11, 27)) is DayKind.WEEKEND  # Friday
        assert gulf.day_kind(date(2026, 11, 29)) is DayKind.TRADING  # Sunday

    def test_has_session_splits_the_four_kinds_correctly(self) -> None:
        assert DayKind.TRADING.has_session
        assert DayKind.EARLY_CLOSE.has_session
        assert not DayKind.HOLIDAY.has_session
        assert not DayKind.WEEKEND.has_session

    def test_a_datetime_passed_to_day_kind_is_refused(self) -> None:
        """datetime subclasses date, so the obvious check accepts one and then
        reads its date in the caller's zone rather than the calendar's."""
        with pytest.raises(CalendarError) as exc:
            calendar_with().day_kind(datetime(2026, 11, 30, 15, 0, tzinfo=UTC))
        assert "local_date" in str(exc.value)

    def test_day_kind_at_uses_the_calendar_zone_not_the_instants(self) -> None:
        """02:00 UTC on the Friday half-day is still Thanksgiving evening in New
        York. Taking .date() off the instant would report a short trading day
        where the market is shut."""
        instant = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
        assert instant.date() == HALF_DAY
        assert calendar_with().local_date(instant) == THANKSGIVING
        assert calendar_with().day_kind_at(instant) is DayKind.HOLIDAY

    def test_closing_time_on_reports_the_wall_clock_close(self) -> None:
        cal = calendar_with()
        assert cal.closing_time_on(MONDAY) == time(16, 0)
        assert cal.closing_time_on(HALF_DAY) == HALF_DAY_CLOSE

    def test_closing_time_on_a_closed_day_is_none(self) -> None:
        cal = calendar_with()
        assert cal.closing_time_on(THANKSGIVING) is None
        assert cal.closing_time_on(date(2026, 11, 28)) is None


# ===========================================================================
# Session resolution
# ===========================================================================


class TestSessionResolution:
    """A session compared as wall-clock times is right for most of the year and
    an hour wrong twice; these pin the actual instants."""

    def test_a_session_resolves_to_utc_instants(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        assert session.opens_at == datetime(2026, 11, 30, 14, 30, tzinfo=UTC)
        assert session.closes_at == datetime(2026, 11, 30, 21, 0, tzinfo=UTC)
        assert session.kind is DayKind.TRADING
        assert session.day == MONDAY

    def test_the_session_keeps_the_declared_wall_clock_hours(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        assert session.opens_at.astimezone(NY).time() == time(9, 30)
        assert session.closes_at.astimezone(NY).time() == time(16, 0)

    def test_the_opening_instant_is_inside_the_session(self) -> None:
        cal = calendar_with()
        assert cal.is_open(datetime(2026, 11, 30, 14, 30, tzinfo=UTC))

    def test_the_closing_instant_is_outside_the_session(self) -> None:
        """Half-open on purpose: an inclusive close would answer 'open' at the
        exact instant next_boundary told the scheduler the session ends, and a
        scheduler that wakes to a contradiction between its own two calls
        spins."""
        cal = calendar_with()
        assert not cal.is_open(datetime(2026, 11, 30, 21, 0, tzinfo=UTC))
        assert cal.is_open(
            datetime(2026, 11, 30, 20, 59, 59, tzinfo=UTC)
        )

    def test_an_instant_before_the_open_is_not_in_a_session(self) -> None:
        cal = calendar_with()
        instant = datetime(2026, 11, 30, 14, 29, 59, tzinfo=UTC)
        assert not cal.is_open(instant)
        assert cal.current_session(instant) is None

    def test_an_instant_after_the_close_is_not_in_a_session(self) -> None:
        cal = calendar_with()
        instant = datetime(2026, 11, 30, 23, 0, tzinfo=UTC)
        assert not cal.is_open(instant)
        assert cal.current_session(instant) is None

    def test_a_weekend_instant_is_never_in_a_session(self) -> None:
        cal = calendar_with()
        # Saturday 28 November 2026, midday New York.
        assert not cal.is_open(datetime(2026, 11, 28, 17, 0, tzinfo=UTC))

    def test_a_holiday_instant_is_never_in_a_session(self) -> None:
        cal = calendar_with()
        assert not cal.is_open(datetime(2026, 11, 26, 17, 0, tzinfo=UTC))

    def test_current_session_returns_the_containing_session(self) -> None:
        cal = calendar_with()
        instant = datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
        session = cal.current_session(instant)
        assert session is not None
        assert session.day == MONDAY
        assert session.contains(instant)

    def test_session_duration_is_the_instant_difference(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        assert session.duration == timedelta(hours=6, minutes=30)

    def test_a_session_cannot_describe_a_closed_day(self) -> None:
        """None, never a zero-length Session: a caller who forgets to check
        should get an AttributeError at the mistake, not a market that reads as
        very quiet."""
        with pytest.raises(CalendarError) as exc:
            Session(
                day=THANKSGIVING,
                kind=DayKind.HOLIDAY,
                opens_at=datetime(2026, 11, 26, 14, 30, tzinfo=UTC),
                closes_at=datetime(2026, 11, 26, 21, 0, tzinfo=UTC),
            )
        assert "holiday day" in str(exc.value)

    def test_a_session_with_a_naive_edge_is_refused(self) -> None:
        with pytest.raises(CalendarError):
            Session(
                day=MONDAY,
                kind=DayKind.TRADING,
                opens_at=datetime(2026, 11, 30, 14, 30),
                closes_at=datetime(2026, 11, 30, 21, 0, tzinfo=UTC),
            )


# ===========================================================================
# Early closes
# ===========================================================================


class TestEarlyClose:
    """An early close that does not actually shorten the session is the failure
    here: the report says half day and the engine trades until four."""

    def test_the_session_ends_at_the_early_time(self) -> None:
        session = calendar_with().session_on(HALF_DAY)
        assert session is not None
        assert session.closes_at == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
        assert session.closes_at.astimezone(NY).time() == HALF_DAY_CLOSE

    def test_the_session_still_opens_at_the_regular_time(self) -> None:
        session = calendar_with().session_on(HALF_DAY)
        assert session is not None
        assert session.opens_at.astimezone(NY).time() == time(9, 30)

    def test_the_session_is_labelled_as_an_early_close(self) -> None:
        session = calendar_with().session_on(HALF_DAY)
        assert session is not None
        assert session.kind is DayKind.EARLY_CLOSE

    def test_the_market_is_shut_after_the_early_close(self) -> None:
        cal = calendar_with()
        assert cal.is_open(datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
        assert not cal.is_open(datetime(2026, 11, 27, 18, 0, tzinfo=UTC))
        # The regular close would still have been open here.
        assert not cal.is_open(datetime(2026, 11, 27, 20, 0, tzinfo=UTC))

    def test_the_early_close_is_shorter_than_a_regular_session(self) -> None:
        cal = calendar_with()
        short = cal.session_on(HALF_DAY)
        regular = cal.session_on(MONDAY)
        assert short is not None and regular is not None
        assert short.duration < regular.duration
        assert short.duration == timedelta(hours=3, minutes=30)


# ===========================================================================
# Daylight saving
# ===========================================================================


class TestDaylightSaving:
    """The whole reason this module exists. A session declared in wall-clock
    time is a different UTC instant on either side of a transition, and code
    that adds 24 hours a day drifts by an hour twice a year -- in the direction
    that makes the engine miss the open."""

    def test_the_utc_offset_differs_across_the_spring_forward(self) -> None:
        cal = calendar_with()
        before = cal.session_on(FRIDAY_EST_MARCH)
        after = cal.session_on(MONDAY_EDT_MARCH)
        assert before is not None and after is not None
        before_offset = before.opens_at.astimezone(NY).utcoffset()
        after_offset = after.opens_at.astimezone(NY).utcoffset()
        assert before_offset == timedelta(hours=-5)
        assert after_offset == timedelta(hours=-4)
        assert before_offset != after_offset

    def test_the_utc_offset_differs_across_the_fall_back(self) -> None:
        cal = calendar_with()
        before = cal.session_on(FRIDAY_EDT_OCTOBER)
        after = cal.session_on(MONDAY_EST_NOVEMBER)
        assert before is not None and after is not None
        assert before.opens_at.astimezone(NY).utcoffset() == timedelta(hours=-4)
        assert after.opens_at.astimezone(NY).utcoffset() == timedelta(hours=-5)

    def test_the_wall_clock_open_is_the_same_on_both_sides(self) -> None:
        """The declared hours do not move; only the instant they name does."""
        cal = calendar_with()
        for day in (
            FRIDAY_EST_MARCH,
            MONDAY_EDT_MARCH,
            FRIDAY_EDT_OCTOBER,
            MONDAY_EST_NOVEMBER,
        ):
            session = cal.session_on(day)
            assert session is not None
            assert session.opens_at.astimezone(NY).time() == time(9, 30), day

    def test_the_utc_open_moves_by_an_hour_across_the_transition(self) -> None:
        """The concrete instants, so a regression cannot hide behind a
        comparison that is itself timezone-aware."""
        cal = calendar_with()
        before = cal.session_on(FRIDAY_EST_MARCH)
        after = cal.session_on(MONDAY_EDT_MARCH)
        assert before is not None and after is not None
        assert before.opens_at == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
        assert after.opens_at == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)

    def test_adding_whole_days_to_the_previous_open_would_be_wrong(self) -> None:
        """The bug this module removes: a scheduler that schedules the next open
        as 'the last one plus 24 hours' is an hour late for the whole summer."""
        cal = calendar_with()
        before = cal.session_on(FRIDAY_EST_MARCH)
        after = cal.session_on(MONDAY_EDT_MARCH)
        assert before is not None and after is not None
        assert after.opens_at != before.opens_at + timedelta(days=3)
        assert after.opens_at == before.opens_at + timedelta(days=3, hours=-1)

    def test_a_session_clear_of_the_transition_keeps_its_length(self) -> None:
        """The New York transitions happen at 02:00 on a Sunday, so a 09:30
        session must not change length just because the week contains one."""
        cal = calendar_with()
        for day in (FRIDAY_EST_MARCH, MONDAY_EDT_MARCH, MONDAY_EST_NOVEMBER):
            session = cal.session_on(day)
            assert session is not None
            assert session.duration == timedelta(hours=6, minutes=30), day

    def test_is_open_is_correct_on_both_sides_of_the_transition(self) -> None:
        cal = calendar_with()
        # 14:00 UTC is 09:00 EST -- before the Friday open.
        assert not cal.is_open(datetime(2026, 3, 6, 14, 0, tzinfo=UTC))
        assert cal.is_open(datetime(2026, 3, 6, 14, 30, tzinfo=UTC))
        # The same UTC clock time on the Monday is 10:00 EDT -- already open.
        assert cal.is_open(datetime(2026, 3, 9, 14, 0, tzinfo=UTC))
        assert not cal.is_open(datetime(2026, 3, 9, 13, 0, tzinfo=UTC))


# ===========================================================================
# Instants are stored in UTC
# ===========================================================================


class TestInstantsAreStoredInUtc:
    """Python compares and subtracts two aware datetimes sharing a tzinfo as if
    they were naive, so a session holding its edges in the market's own zone
    measures clock faces rather than time -- and only during the two weeks a
    year when that differs, which is when a scheduler is least likely to be
    watched."""

    def test_session_edges_are_utc(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        assert session.opens_at.utcoffset() == timedelta(0)
        assert session.closes_at.utcoffset() == timedelta(0)

    def test_the_local_date_is_kept_alongside_the_utc_edges(self) -> None:
        """UTC instants plus the local date they belong to, so neither has to be
        re-derived by a caller who would have to pick a zone to do it in."""
        session = calendar_with().session_on(HALF_DAY)
        assert session is not None
        assert session.day == HALF_DAY
        assert session.opens_at.astimezone(NY).date() == HALF_DAY

    def test_same_zone_subtraction_is_the_trap_being_avoided(self) -> None:
        """The demonstration: two New York datetimes spanning the fall back
        subtract to their clock-face difference, an hour short of the truth."""
        opens_local = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=0)
        closes_local = datetime(2026, 11, 1, 5, 0, tzinfo=NY)
        assert closes_local - opens_local == timedelta(hours=3, minutes=30)
        session = night_calendar(time(1, 30), time(5, 0)).session_on(FALL_BACK)
        assert session is not None
        assert session.duration == timedelta(hours=4, minutes=30)

    def test_contains_is_correct_for_an_instant_in_the_markets_own_zone(
        self,
    ) -> None:
        """01:15 on the fall-back morning happens twice: once before the session
        opens and once after. A same-zone comparison would ignore the fold and
        answer identically for both."""
        cal = night_calendar(time(1, 30), time(5, 0))
        before_open = datetime(2026, 11, 1, 1, 15, tzinfo=NY, fold=0)
        after_open = datetime(2026, 11, 1, 1, 15, tzinfo=NY, fold=1)
        assert before_open.timetuple()[:6] == after_open.timetuple()[:6]
        assert not cal.is_open(before_open)
        assert cal.is_open(after_open)


# ===========================================================================
# Ambiguous and nonexistent local times
# ===========================================================================


class TestAmbiguousLocalTimes:
    """The hour that happens twice. Choosing the second occurrence would leave a
    scheduler asleep for an hour while the session it waits for is trading."""

    def test_a_repeated_local_time_resolves_to_its_first_occurrence(self) -> None:
        session = night_calendar(time(1, 30), time(5, 0)).session_on(FALL_BACK)
        assert session is not None
        # 01:30 EDT, the first time the clock reads it.
        assert session.opens_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        assert session.opens_at.astimezone(NY).utcoffset() == timedelta(hours=-4)
        assert session.opens_at.astimezone(NY).time() == time(1, 30)

    def test_the_second_occurrence_is_a_real_instant_that_is_not_chosen(
        self,
    ) -> None:
        """Both readings exist; the module picks one deliberately rather than
        inheriting whatever fold happened to be set."""
        second = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=1)
        assert second.astimezone(timezone.utc) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        session = night_calendar(time(1, 30), time(5, 0)).session_on(FALL_BACK)
        assert session is not None
        assert session.opens_at != second.astimezone(timezone.utc)

    def test_a_session_spanning_the_fall_back_is_an_hour_longer_than_its_face(
        self,
    ) -> None:
        """01:30 to 05:00 reads as three and a half hours on the clock and is
        four and a half in real time. A scheduler sizing a sleep off the clock
        face wakes an hour early."""
        session = night_calendar(time(1, 30), time(5, 0)).session_on(FALL_BACK)
        assert session is not None
        assert session.duration == timedelta(hours=4, minutes=30)

    def test_the_repeated_hour_is_inside_the_session_both_times(self) -> None:
        cal = night_calendar(time(1, 30), time(5, 0))
        first_pass = datetime(2026, 11, 1, 5, 45, tzinfo=UTC)  # 01:45 EDT
        second_pass = datetime(2026, 11, 1, 6, 45, tzinfo=UTC)  # 01:45 EST
        assert first_pass.astimezone(NY).time() == time(1, 45)
        assert second_pass.astimezone(NY).time() == time(1, 45)
        assert cal.is_open(first_pass)
        assert cal.is_open(second_pass)

    def test_an_instant_before_the_first_occurrence_is_still_closed(self) -> None:
        cal = night_calendar(time(1, 30), time(5, 0))
        assert not cal.is_open(datetime(2026, 11, 1, 5, 29, tzinfo=UTC))


class TestNonexistentLocalTimes:
    """The hour that never happens. Python's default arithmetic silently invents
    an instant an hour past the one asked for; this module refuses instead."""

    def test_a_local_time_that_never_happens_is_refused(self) -> None:
        with pytest.raises(CalendarError) as exc:
            night_calendar(time(2, 30), time(6, 0)).session_on(SPRING_FORWARD)
        assert "does not exist" in str(exc.value)

    def test_the_refusal_names_the_date_the_time_and_the_zone(self) -> None:
        """A refusal a caller cannot act on is an outage with better manners."""
        with pytest.raises(CalendarError) as exc:
            night_calendar(time(2, 30), time(6, 0)).session_on(SPRING_FORWARD)
        message = str(exc.value)
        assert "02:30" in message
        assert "2026-03-08" in message
        assert "America/New_York" in message
        assert "hint" in message

    def test_python_default_arithmetic_would_have_moved_it_silently(self) -> None:
        """The behaviour being refused: 02:30 becomes 03:30 with no error, so an
        unguarded implementation opens an hour late on exactly one day a year.

        The round trip through UTC is what exposes it -- ``astimezone`` back into
        the zone a datetime already carries is a no-op, so the invented instant
        keeps wearing the clock face that never happened until something
        converts it."""
        stamped = datetime(2026, 3, 8, 2, 30, tzinfo=NY)
        assert stamped.astimezone(NY).time() == time(2, 30)
        assert stamped.astimezone(timezone.utc) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
        assert stamped.astimezone(timezone.utc).astimezone(NY).time() == time(3, 30)

    def test_the_days_either_side_of_the_gap_resolve_normally(self) -> None:
        """The refusal must be about that one date, not about the zone."""
        cal = night_calendar(time(2, 30), time(6, 0))
        for day in (date(2026, 3, 7), date(2026, 3, 9)):
            session = cal.session_on(day)
            assert session is not None
            assert session.opens_at.astimezone(NY).time() == time(2, 30), day

    def test_a_nonexistent_close_is_refused_too(self) -> None:
        cal = night_calendar(time(1, 0), time(2, 30))
        with pytest.raises(CalendarError) as exc:
            cal.session_on(SPRING_FORWARD)
        assert "does not exist" in str(exc.value)

    def test_next_session_surfaces_the_gap_rather_than_skipping_it(self) -> None:
        """Skipping the date would silently drop a session the operator
        declared, which is the failure the refusal exists to make loud."""
        cal = night_calendar(time(2, 30), time(6, 0))
        with pytest.raises(CalendarError):
            cal.next_session(datetime(2026, 3, 7, 12, 0, tzinfo=UTC))

    def test_a_session_clear_of_the_gap_is_unaffected(self) -> None:
        """The ordinary 09:30 calendar must not pay for this on the same date;
        the Sunday has no session at all."""
        assert calendar_with().session_on(SPRING_FORWARD) is None


# ===========================================================================
# Naive datetimes
# ===========================================================================


class TestNaiveDatetimesAreRefused:
    """A naive timestamp is a defect, not an input. Assuming UTC or assuming
    local shifts the instant by the offset, and 'the market is open' is then
    wrong for exactly as long as the guess was."""

    NAIVE = datetime(2026, 11, 30, 15, 0)

    def test_every_instant_taking_method_refuses_a_naive_datetime(self) -> None:
        cal = calendar_with()
        for method in (
            cal.local_date,
            cal.day_kind_at,
            cal.is_open,
            cal.current_session,
            cal.next_session,
            cal.next_boundary,
            cal.time_until_next_boundary,
        ):
            with pytest.raises(CalendarError) as exc:
                method(self.NAIVE)  # type: ignore[operator]
            assert "timezone-aware" in str(exc.value), method

    def test_the_refusal_is_not_a_silent_utc_assumption(self) -> None:
        """The same clock face answers when it carries a zone and refuses when
        it does not -- proof the module is not quietly stamping one on."""
        cal = calendar_with()
        aware = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
        assert cal.is_open(aware)
        with pytest.raises(CalendarError):
            cal.is_open(aware.replace(tzinfo=None))

    def test_session_contains_refuses_a_naive_datetime(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        with pytest.raises(CalendarError):
            session.contains(self.NAIVE)

    def test_boundary_time_from_refuses_a_naive_datetime(self) -> None:
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
        )
        with pytest.raises(CalendarError):
            boundary.time_from(self.NAIVE)

    def test_a_string_instead_of_a_datetime_is_refused_not_an_attributeerror(
        self,
    ) -> None:
        """Reading .tzinfo off a string raises AttributeError, which escapes the
        CalendarError contract a caller catches to report the problem."""
        with pytest.raises(CalendarError) as exc:
            calendar_with().is_open("2026-11-30T15:00:00Z")  # type: ignore[arg-type]
        assert "must be a datetime" in str(exc.value)

    def test_a_date_instead_of_a_datetime_is_refused(self) -> None:
        with pytest.raises(CalendarError):
            calendar_with().is_open(MONDAY)  # type: ignore[arg-type]


# ===========================================================================
# Next session and next boundary
# ===========================================================================


class TestNextSessionAndBoundary:
    """What a scheduler actually calls. A boundary in the past or on the wrong
    side of a close is a sleep that returns immediately, forever."""

    def test_before_the_open_the_boundary_is_todays_open(self) -> None:
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
        )
        assert boundary.kind is BoundaryKind.OPEN
        assert boundary.at == datetime(2026, 11, 30, 14, 30, tzinfo=UTC)
        assert boundary.session.day == MONDAY

    def test_inside_a_session_the_boundary_is_its_close(self) -> None:
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
        )
        assert boundary.kind is BoundaryKind.CLOSE
        assert boundary.at == datetime(2026, 11, 30, 21, 0, tzinfo=UTC)

    def test_at_the_opening_instant_the_boundary_is_already_the_close(self) -> None:
        """The half-open interval decides the tie; both cases must not claim
        the same instant."""
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 30, 14, 30, tzinfo=UTC)
        )
        assert boundary.kind is BoundaryKind.CLOSE

    def test_at_the_closing_instant_the_boundary_is_the_next_open(self) -> None:
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 30, 21, 0, tzinfo=UTC)
        )
        assert boundary.kind is BoundaryKind.OPEN
        assert boundary.at == datetime(2026, 12, 1, 14, 30, tzinfo=UTC)
        assert boundary.session.day == TUESDAY

    def test_the_boundary_skips_the_weekend(self) -> None:
        """Friday evening to Monday morning, not Saturday morning."""
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 27, 22, 0, tzinfo=UTC)
        )
        assert boundary.session.day == MONDAY
        assert boundary.at == datetime(2026, 11, 30, 14, 30, tzinfo=UTC)

    def test_the_boundary_skips_a_holiday(self) -> None:
        """Wednesday evening lands on the Friday half day: Thanksgiving is
        Thursday."""
        boundary = calendar_with().next_boundary(
            datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
        )
        assert boundary.session.day == HALF_DAY
        assert boundary.session.kind is DayKind.EARLY_CLOSE
        assert boundary.at == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)

    def test_next_session_is_strictly_after_the_instant(self) -> None:
        """Mid-session, 'the next open' means tomorrow. current_session answers
        the other question, and one method could only be wrong for one of
        them."""
        cal = calendar_with()
        instant = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
        current = cal.current_session(instant)
        upcoming = cal.next_session(instant)
        assert current is not None
        assert current.day == MONDAY
        assert upcoming.day == TUESDAY
        assert upcoming.opens_at > instant

    def test_next_session_can_be_today_when_the_open_has_not_happened(self) -> None:
        upcoming = calendar_with().next_session(
            datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
        )
        assert upcoming.day == MONDAY

    def test_the_sleep_is_the_distance_to_the_boundary(self) -> None:
        cal = calendar_with()
        instant = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
        assert cal.time_until_next_boundary(instant) == timedelta(hours=6)

    def test_the_sleep_over_a_weekend_covers_the_whole_gap(self) -> None:
        """From the Friday early close to the Monday open: two days and twenty
        and a half hours, not the sixty-five a naive day count would give."""
        cal = calendar_with()
        instant = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
        assert cal.time_until_next_boundary(instant) == timedelta(
            days=2, hours=20, minutes=30
        )

    def test_the_sleep_is_always_strictly_positive(self) -> None:
        """A zero or negative sleep is a busy spin, and a scheduler that guards
        for it usually forgets to."""
        cal = calendar_with()
        for instant in (
            datetime(2026, 11, 30, 14, 29, 59, tzinfo=UTC),
            datetime(2026, 11, 30, 14, 30, tzinfo=UTC),
            datetime(2026, 11, 30, 20, 59, 59, tzinfo=UTC),
            datetime(2026, 11, 30, 21, 0, tzinfo=UTC),
            datetime(2026, 11, 28, 17, 0, tzinfo=UTC),
            datetime(2026, 11, 26, 17, 0, tzinfo=UTC),
        ):
            assert cal.time_until_next_boundary(instant) > timedelta(0), instant

    def test_a_boundary_off_its_own_session_is_refused(self) -> None:
        """A boundary that does not sit on its session's edge would send a
        scheduler to sleep until the wrong instant."""
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        with pytest.raises(CalendarError) as exc:
            SessionBoundary(
                at=session.closes_at, kind=BoundaryKind.OPEN, session=session
            )
        assert "not the session's open" in str(exc.value)

    def test_a_calendar_that_never_opens_again_is_reported_not_hung(self) -> None:
        """An unbounded scan would express a broken holiday list as a hang --
        the one failure a scheduler cannot report."""
        closed = MarketCalendar(
            timezone=NY,
            regular_hours=REGULAR,
            weekend_days=SATURDAY_AND_SUNDAY,
            holidays=tuple(
                date(2026, 1, 1) + timedelta(days=offset) for offset in range(400)
            ),
            early_closes=(),
        )
        with pytest.raises(CalendarError) as exc:
            closed.next_session(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
        assert str(MAX_SESSION_SEARCH_DAYS) in str(exc.value)


# ===========================================================================
# Immutability
# ===========================================================================


class TestImmutability:
    """The calendar that said 'open' must be the calendar a decision is recorded
    against; a mutable one makes that claim unprovable."""

    def test_assignment_to_a_calendar_field_raises(self) -> None:
        cal = calendar_with()
        with pytest.raises(Exception):
            cal.holidays = ()  # type: ignore[misc]
        assert THANKSGIVING in cal.holidays

    def test_the_calendar_is_hashable(self) -> None:
        """Tuples rather than sets or dicts, precisely so an instance can be a
        key in a decision record."""
        assert isinstance(hash(calendar_with()), int)
        assert len({calendar_with(), calendar_with()}) == 1

    def test_equal_calendars_compare_and_hash_equally(self) -> None:
        assert calendar_with() == calendar_with()
        assert hash(calendar_with()) == hash(calendar_with())

    def test_a_different_holiday_list_is_a_different_calendar(self) -> None:
        assert calendar_with() != calendar_with(holidays=(), early_closes=())

    def test_a_session_is_frozen(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        with pytest.raises(Exception):
            session.closes_at = session.opens_at  # type: ignore[misc]


class TestTheUtcInvariantIsEnforcedNotAssumed:
    """A Session built by hand must not be able to reproduce the DST bug.

    Everything Session promises about duration and containment depends on its
    edges having no transitions in their own zone. The factory always produced
    UTC, but the type accepted anything aware -- and a caller can construct one
    directly. An invariant stated only in a docstring is a hypothesis.
    """

    def test_edges_given_in_a_market_zone_are_normalised_to_utc(self) -> None:
        zone = NY
        session = Session(
            day=FALL_BACK,
            kind=DayKind.TRADING,
            opens_at=datetime(2026, 11, 1, 0, 30, tzinfo=zone),
            closes_at=datetime(2026, 11, 1, 4, 30, tzinfo=zone),
        )

        assert session.opens_at.tzinfo is timezone.utc
        assert session.closes_at.tzinfo is timezone.utc

    def test_a_fall_back_session_built_by_hand_reports_real_elapsed_time(self) -> None:
        """Same wall-clock faces, an hour apart in reality. Stored in the market's
        own zone, Python subtracts the clock faces and reports four hours."""
        zone = NY
        opens = datetime(2026, 11, 1, 0, 30, tzinfo=zone)
        closes = datetime(2026, 11, 1, 4, 30, tzinfo=zone)

        session = Session(
            day=FALL_BACK, kind=DayKind.TRADING, opens_at=opens, closes_at=closes
        )

        assert session.duration == closes.astimezone(timezone.utc) - opens.astimezone(timezone.utc)
        assert session.duration == timedelta(hours=5), (
            "the naive subtraction of two same-zone faces would say 4 hours"
        )

    def test_containment_distinguishes_both_passes_of_a_repeated_hour(self) -> None:
        zone = NY
        # 00:30 EDT = 04:30Z; 01:00 EST (the SECOND pass of 01:00) = 06:00Z.
        # The session therefore spans the repeated hour.
        session = Session(
            day=FALL_BACK,
            kind=DayKind.TRADING,
            opens_at=datetime(2026, 11, 1, 0, 30, tzinfo=zone),
            closes_at=datetime(2026, 11, 1, 1, 0, tzinfo=zone, fold=1),
        )
        first_pass = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)  # 05:30Z, inside
        second_pass = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)  # 06:30Z, outside

        assert session.contains(first_pass) is not session.contains(second_pass), (
            "the two passes of the repeated hour are different instants"
        )


# ===========================================================================
# Boundary instants
# ===========================================================================


class TestBoundaryInstantsAreNormalisedToUtc:
    """A SessionBoundary built by hand must not be able to reproduce the DST bug.

    ``time_from`` is the sleep a scheduler is sized from, and Python subtracts
    two aware datetimes sharing a ``tzinfo`` as if they were naive. A boundary
    quoted in the market's own zone therefore measures clock faces whenever the
    caller's instant carries that same zone: an hour out across a transition,
    and identical for both passes of a repeated hour -- one of which is a sleep
    to an instant already past. The boundary's own edge check does not catch it,
    for two separate reasons pinned below.
    """

    def test_a_boundary_given_in_the_market_zone_is_stored_as_utc(self) -> None:
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        boundary = SessionBoundary(
            at=session.opens_at.astimezone(NY),
            kind=BoundaryKind.OPEN,
            session=session,
        )
        assert boundary.at.tzinfo is timezone.utc, (
            "a boundary holding a market-zone datetime subtracts clock faces"
        )
        assert boundary.at == datetime(2026, 11, 30, 14, 30, tzinfo=UTC)

    def test_the_edge_check_compares_instants_so_it_cannot_be_the_guard(self) -> None:
        """Why the un-normalised value survives construction at all: a
        market-zone datetime and the session's UTC edge are the same instant, so
        the check that a boundary sits on its own edge passes and stores it."""
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        quoted_locally = session.opens_at.astimezone(NY)
        assert quoted_locally == session.opens_at, (
            "cross-zone comparison is already instant comparison"
        )
        assert quoted_locally.tzinfo is not timezone.utc
        boundary = SessionBoundary(
            at=quoted_locally, kind=BoundaryKind.OPEN, session=session
        )
        assert boundary.at == session.opens_at

    def test_a_naive_boundary_instant_is_refused_not_assumed_to_be_local(self) -> None:
        """The aware check must run before the conversion. ``astimezone`` on a
        naive datetime assumes the *system's* zone, so a reordering here would
        turn a defect into an instant shifted by whatever the host is set to."""
        session = calendar_with().session_on(MONDAY)
        assert session is not None
        with pytest.raises(CalendarError) as exc:
            SessionBoundary(
                at=session.opens_at.replace(tzinfo=None),
                kind=BoundaryKind.OPEN,
                session=session,
            )
        assert "timezone-aware" in str(exc.value)

    def test_time_from_across_a_spring_forward_measures_elapsed_time(self) -> None:
        """Friday noon in New York to the Monday open, over the 8 March gap. The
        clock faces are three days and 21:30 apart; only 20:30 of the last day
        actually happens, and a scheduler sleeping the clock-face figure wakes an
        hour after the open."""
        session = calendar_with().session_on(MONDAY_EDT_MARCH)
        assert session is not None
        boundary = SessionBoundary(
            at=session.opens_at.astimezone(NY),
            kind=BoundaryKind.OPEN,
            session=session,
        )
        instant = datetime(2026, 3, 6, 12, 0, tzinfo=NY)

        assert boundary.time_from(instant) == timedelta(
            days=2, hours=20, minutes=30
        ), "the subtraction of two same-zone faces would say 2 days 21:30"
        assert boundary.time_from(instant) == session.opens_at - instant.astimezone(
            timezone.utc
        )

    def test_a_boundary_on_the_spring_forward_transition_reports_the_real_gap(
        self,
    ) -> None:
        """A close landing exactly on the transition instant: 03:00 EDT is the
        first wall-clock reading after the skip, and 07:00Z is the instant the
        clock jumps. From 01:30 EST the market has half an hour left, not the
        hour and a half the clock face claims."""
        cal = night_calendar(time(1, 0), time(3, 0))
        session = cal.session_on(SPRING_FORWARD)
        assert session is not None
        assert session.closes_at == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
        assert session.duration == timedelta(hours=1), (
            "01:00 to 03:00 reads as two hours and is one; the gap ate the rest"
        )

        boundary = SessionBoundary(
            at=session.closes_at.astimezone(NY),
            kind=BoundaryKind.CLOSE,
            session=session,
        )
        assert boundary.at.astimezone(NY).time() == time(3, 0)
        assert boundary.time_from(
            datetime(2026, 3, 8, 1, 30, tzinfo=NY)
        ) == timedelta(minutes=30), "the clock faces are 90 minutes apart"

    def test_an_ambiguous_boundary_is_not_rejected_by_its_own_session(self) -> None:
        """The second hazard, and it fails in the opposite direction. Under
        PEP 495 an ambiguous datetime compares *unequal* to any datetime in a
        different zone, so a boundary quoted as the second reading of a repeated
        hour is refused by the very edge check meant to confirm it sits on that
        edge -- a scheduler told its own session's close is not its close."""
        second_reading = datetime(2026, 11, 1, 1, 0, tzinfo=NY, fold=1)
        session = Session(
            day=FALL_BACK,
            kind=DayKind.TRADING,
            opens_at=datetime(2026, 11, 1, 0, 30, tzinfo=NY),
            closes_at=second_reading,
        )
        assert second_reading != session.closes_at, (
            "PEP 495: an ambiguous local time is unequal across zones"
        )
        assert second_reading.astimezone(timezone.utc) == session.closes_at

        boundary = SessionBoundary(
            at=second_reading, kind=BoundaryKind.CLOSE, session=session
        )
        assert boundary.at == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)

    def test_both_passes_of_the_repeated_hour_give_different_sleeps(self) -> None:
        """01:30 happens twice on 1 November; the close is the transition instant
        between them. The first pass has half an hour to run and the second is
        half an hour late, and a boundary in the market's own zone answers minus
        thirty minutes to both -- so a scheduler that woke at the first pass is
        told it has already missed the close."""
        session = Session(
            day=FALL_BACK,
            kind=DayKind.TRADING,
            opens_at=datetime(2026, 11, 1, 0, 30, tzinfo=NY),
            closes_at=datetime(2026, 11, 1, 1, 0, tzinfo=NY, fold=1),
        )
        boundary = SessionBoundary(
            at=datetime(2026, 11, 1, 1, 0, tzinfo=NY, fold=1),
            kind=BoundaryKind.CLOSE,
            session=session,
        )
        first_pass = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=0)  # 05:30Z
        second_pass = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=1)  # 06:30Z
        assert first_pass.timetuple()[:6] == second_pass.timetuple()[:6]

        assert boundary.time_from(first_pass) == timedelta(minutes=30)
        assert boundary.time_from(second_pass) == timedelta(minutes=-30)
        assert boundary.time_from(first_pass) != boundary.time_from(second_pass), (
            "the two passes are an hour apart; a clock-face subtraction says "
            "minus thirty minutes to both"
        )

    def test_a_hand_built_boundary_matches_the_one_the_calendar_produces(
        self,
    ) -> None:
        """The factory and the constructor must not disagree about the instant or
        about the zone it is carried in."""
        cal = calendar_with()
        instant = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
        from_calendar = cal.next_boundary(instant)
        by_hand = SessionBoundary(
            at=from_calendar.at.astimezone(NY),
            kind=BoundaryKind.CLOSE,
            session=from_calendar.session,
        )
        assert by_hand.at == from_calendar.at
        assert by_hand.at.tzinfo is from_calendar.at.tzinfo is timezone.utc
        assert by_hand.time_from(instant) == from_calendar.time_from(instant)


class TestACloseInsideTheSpringForwardGap:
    """A close that never happens must not become a boundary.

    The open landing in the gap is refused where it is resolved; the close is
    the same refusal reached through a different call, and the failure it
    prevents is worse -- Python's own arithmetic maps 02:30 to 03:30 without
    complaint, so an unguarded module hands a scheduler a close an hour past the
    one the operator declared, on exactly one date a year.
    """

    def test_no_boundary_is_produced_from_a_close_that_never_happens(self) -> None:
        cal = night_calendar(time(1, 0), time(2, 30))
        invented = datetime(2026, 3, 8, 2, 30, tzinfo=NY).astimezone(timezone.utc)
        assert invented.astimezone(NY).time() == time(3, 30), (
            "python's arithmetic moves the declared close an hour forward"
        )

        with pytest.raises(CalendarError) as exc:
            cal.next_boundary(datetime(2026, 3, 8, 9, 0, tzinfo=UTC))
        message = str(exc.value)
        assert "does not exist" in message
        assert "02:30" in message

    def test_the_sleep_lookup_refuses_on_the_gap_date_too(self) -> None:
        """time_until_next_boundary is the call a scheduler actually makes; it
        must not be the one path that survives the gap."""
        cal = night_calendar(time(1, 0), time(2, 30))
        with pytest.raises(CalendarError):
            cal.time_until_next_boundary(datetime(2026, 3, 8, 9, 0, tzinfo=UTC))

    def test_the_scan_stops_at_the_gap_rather_than_stepping_over_it(self) -> None:
        """Stepping to the next day would silently drop the declared session and
        answer with the day after's hours, which reads as an ordinary result."""
        cal = night_calendar(time(1, 0), time(2, 30))
        with pytest.raises(CalendarError):
            cal.next_session(datetime(2026, 3, 7, 12, 0, tzinfo=UTC))

    def test_only_that_date_is_refused(self) -> None:
        """The refusal is about the transition, not about the hours or the zone."""
        cal = night_calendar(time(1, 0), time(2, 30))
        for day in (date(2026, 3, 7), date(2026, 3, 9)):
            session = cal.session_on(day)
            assert session is not None, day
            assert session.closes_at.astimezone(NY).time() == time(2, 30), day
