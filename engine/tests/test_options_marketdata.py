"""Market-data provenance, greek normalization, and the live-data gate.

The negative cases here are the point. Each one corresponds to a way the
obvious implementation would have said "live" about data that was not.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from engine.errors import MarketDataRefusedError, RefusedError
from engine.options.marketdata import (
    GREEK_SENTINEL,
    IB_UNSET,
    Liveness,
    MarketDataProvenance,
    MarketDataSubscription,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    RefusalReason,
    UnderlyingQuote,
    normalize_greek,
    require_live_quote,
    require_uniform_live_provenance,
)

NOW = datetime(2026, 7, 29, 14, 30, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(seconds=5)
D = Decimal


class FakeComputation:
    """Shaped like ib_async's OptionComputation, including its field names."""

    def __init__(self, **kwargs) -> None:
        self.impliedVol = kwargs.get("impliedVol")
        self.delta = kwargs.get("delta")
        self.gamma = kwargs.get("gamma")
        self.vega = kwargs.get("vega")
        self.theta = kwargs.get("theta")
        self.undPrice = kwargs.get("undPrice")


def live_provenance(
    generation=None, *, event_at=None, reported=MarketDataType.LIVE
) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=MarketDataType.LIVE,
        subscription_generation=generation or uuid4(),
        subscribed_at=NOW - timedelta(seconds=30),
        reported_type=int(reported) if reported is not None else None,
        callback_received=reported is not None,
        last_provider_event_at=event_at or NOW - timedelta(seconds=1),
        last_local_receive_at=NOW - timedelta(seconds=1),
    )


def greeks(generation, *, delta="0.16") -> OptionGreeks:
    return OptionGreeks(
        received_at=NOW - timedelta(seconds=1),
        subscription_generation=generation,
        implied_volatility=D("0.22"),
        delta=D(delta) if delta is not None else None,
        underlying_price=D("500"),
    )


# ===========================================================================
# Greek normalization -- the sentinels
# ===========================================================================


class TestNormalizeGreek:
    def test_theta_sentinel_becomes_none(self) -> None:
        """ib_async wrapper.py:1390-1391 never nulls this; -2.0 is finite and
        looks like a plausible theta."""
        assert normalize_greek(GREEK_SENTINEL, "theta") is None

    def test_vega_sentinel_becomes_none(self) -> None:
        assert normalize_greek(GREEK_SENTINEL, "vega") is None

    def test_gamma_sentinel_becomes_none(self) -> None:
        assert normalize_greek(GREEK_SENTINEL, "gamma") is None

    def test_dbl_max_becomes_none_for_every_field(self) -> None:
        """IBKR's 'field does not apply'. Finite, so isfinite() passes it."""
        for name in ("delta", "theta", "vega", "gamma", "implied_volatility"):
            assert normalize_greek(IB_UNSET, name) is None, name

    def test_nan_and_infinity_become_none(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            assert normalize_greek(value, "delta") is None

    def test_none_stays_none(self) -> None:
        assert normalize_greek(None, "delta") is None

    def test_delta_outside_unit_range_rejected(self) -> None:
        assert normalize_greek(1.5, "delta") is None
        assert normalize_greek(-1.5, "delta") is None

    def test_delta_at_the_boundaries_is_accepted(self) -> None:
        assert normalize_greek(1.0, "delta") == D("1.0")
        assert normalize_greek(-1.0, "delta") == D("-1.0")

    def test_zero_delta_is_a_real_value_not_absence(self) -> None:
        """A far OTM option genuinely has a near-zero delta."""
        assert normalize_greek(0.0, "delta") == D("0.0")

    def test_negative_implied_volatility_rejected(self) -> None:
        assert normalize_greek(-0.2, "implied_volatility") is None

    def test_nonpositive_underlying_price_rejected(self) -> None:
        assert normalize_greek(0.0, "underlying_price") is None

    def test_valid_theta_survives(self) -> None:
        assert normalize_greek(-0.045, "theta") == D("-0.045")

    def test_bool_is_not_a_greek(self) -> None:
        assert normalize_greek(True, "delta") is None  # type: ignore[arg-type]

    def test_result_is_decimal_via_string(self) -> None:
        """Decimal(str(f)) avoids dragging binary float noise into risk math."""
        assert normalize_greek(0.16, "delta") == D("0.16")


class TestGreeksFromIB:
    def test_sentinels_do_not_reach_the_snapshot(self) -> None:
        gen = uuid4()
        computed = OptionGreeks.from_ib(
            FakeComputation(
                delta=-0.16, theta=GREEK_SENTINEL, vega=GREEK_SENTINEL, impliedVol=0.22
            ),
            received_at=NOW,
            subscription_generation=gen,
        )
        assert computed.delta == D("-0.16")
        assert computed.theta is None
        assert computed.vega is None
        assert computed.has_valid_delta

    def test_computation_present_but_delta_absent(self) -> None:
        """wrapper.py:1383-1393 assigns the computation even when every field
        sanitizes away, so presence is not validity."""
        computed = OptionGreeks.from_ib(
            FakeComputation(theta=GREEK_SENTINEL),
            received_at=NOW,
            subscription_generation=uuid4(),
        )
        assert computed is not None
        assert not computed.has_valid_delta

    def test_missing_attributes_do_not_raise(self) -> None:
        computed = OptionGreeks.from_ib(
            object(), received_at=NOW, subscription_generation=uuid4()
        )
        assert computed.delta is None


# ===========================================================================
# Provenance classification
# ===========================================================================


class TestProvenance:
    def test_absent_callback_is_unknown_not_live(self) -> None:
        """The whole reason this module exists: Ticker.marketDataType defaults
        to 1, so 'no callback' must not read as 'live'."""
        prov = MarketDataProvenance(
            requested_type=MarketDataType.LIVE,
            subscription_generation=uuid4(),
            subscribed_at=NOW,
        )
        assert prov.liveness is Liveness.UNKNOWN
        assert not prov.is_live

    def test_requested_live_does_not_make_it_live(self) -> None:
        prov = MarketDataProvenance(
            requested_type=MarketDataType.LIVE,
            subscription_generation=uuid4(),
            subscribed_at=NOW,
            reported_type=int(MarketDataType.DELAYED),
            callback_received=True,
        )
        assert prov.requested_type == MarketDataType.LIVE
        assert prov.liveness is Liveness.DELAYED

    def test_each_reported_type_classifies(self) -> None:
        expected = {
            1: Liveness.LIVE,
            2: Liveness.FROZEN,
            3: Liveness.DELAYED,
            4: Liveness.DELAYED_FROZEN,
        }
        for code, liveness in expected.items():
            prov = live_provenance(reported=code)
            assert prov.liveness is liveness, code

    def test_unrecognised_type_is_unknown(self) -> None:
        assert live_provenance(reported=99).liveness is Liveness.UNKNOWN

    def test_age_measured_from_provider_not_local_receipt(self) -> None:
        """A delayed quote arrives promptly; local receipt would look fresh."""
        prov = MarketDataProvenance(
            requested_type=1,
            subscription_generation=uuid4(),
            subscribed_at=NOW,
            reported_type=1,
            callback_received=True,
            last_provider_event_at=NOW - timedelta(minutes=15),
            last_local_receive_at=NOW,
        )
        assert prov.age_at(NOW) == timedelta(minutes=15)

    def test_age_is_none_without_a_provider_timestamp(self) -> None:
        prov = MarketDataProvenance(
            requested_type=1,
            subscription_generation=uuid4(),
            subscribed_at=NOW,
            reported_type=1,
            callback_received=True,
        )
        assert prov.age_at(NOW) is None


# ===========================================================================
# Subscription generations
# ===========================================================================


class TestSubscriptionGenerations:
    def test_restart_mints_a_new_generation(self) -> None:
        sub = MarketDataSubscription(requested_type=1, subscribed_at=NOW)
        first = sub.generation
        second = sub.restart(requested_type=3, at=NOW)
        assert first != second
        assert sub.generation == second

    def test_restart_discards_everything_observed(self) -> None:
        """The stale-greeks defence: ib_async would still be holding the old
        modelGreeks on the reused ticker."""
        sub = MarketDataSubscription(requested_type=1, subscribed_at=NOW)
        sub.record_data_type(1, at=NOW)
        sub.record_greeks(FakeComputation(delta=-0.16), at=NOW)
        sub.record_provider_event(NOW)
        assert sub.current_greeks() is not None

        sub.restart(requested_type=3, at=NOW)
        assert sub.current_greeks() is None
        assert sub.reported_type is None
        assert sub.callback_received is False
        assert sub.last_provider_event_at is None

    def test_greeks_from_a_superseded_generation_are_dropped(self) -> None:
        sub = MarketDataSubscription(requested_type=1, subscribed_at=NOW)
        stale_generation = uuid4()
        sub.record_greeks(
            FakeComputation(delta=-0.16), at=NOW, generation=stale_generation
        )
        assert sub.current_greeks() is None

    def test_greeks_stamped_with_the_current_generation_are_kept(self) -> None:
        sub = MarketDataSubscription(requested_type=1, subscribed_at=NOW)
        sub.record_greeks(
            FakeComputation(delta=-0.16), at=NOW, generation=sub.generation
        )
        current = sub.current_greeks()
        assert current is not None
        assert current.subscription_generation == sub.generation

    def test_only_the_data_type_callback_sets_callback_received(self) -> None:
        sub = MarketDataSubscription(requested_type=1, subscribed_at=NOW)
        sub.record_greeks(FakeComputation(delta=-0.16), at=NOW)
        sub.record_provider_event(NOW)
        assert sub.callback_received is False
        assert sub.provenance().liveness is Liveness.UNKNOWN

        sub.record_data_type(1, at=NOW)
        assert sub.provenance().liveness is Liveness.LIVE


# ===========================================================================
# The live-data gate
# ===========================================================================


class TestRequireLiveQuote:
    def _call(self, prov, *, generation=None, at=NOW, max_age=MAX_AGE):
        require_live_quote(
            prov,
            decision_time=at,
            maximum_age=max_age,
            active_generation=generation or prov.subscription_generation,
            label="test",
        )

    def test_live_current_quote_passes(self) -> None:
        self._call(live_provenance())

    def test_missing_callback_refused(self) -> None:
        prov = MarketDataProvenance(
            requested_type=1, subscription_generation=uuid4(), subscribed_at=NOW
        )
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(prov)
        assert exc.value.reason == RefusalReason.NO_DATA_TYPE_CALLBACK.value

    def test_delayed_refused_with_the_realtime_reason(self) -> None:
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(live_provenance(reported=MarketDataType.DELAYED))
        assert exc.value.reason == "OPTIONS_REALTIME_DATA_REQUIRED"

    def test_frozen_refused(self) -> None:
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(live_provenance(reported=MarketDataType.FROZEN))
        assert exc.value.reason == "OPTIONS_REALTIME_DATA_REQUIRED"

    def test_delayed_frozen_refused(self) -> None:
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(live_provenance(reported=MarketDataType.DELAYED_FROZEN))
        assert exc.value.reason == "OPTIONS_REALTIME_DATA_REQUIRED"

    def test_generation_mismatch_refused(self) -> None:
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(live_provenance(), generation=uuid4())
        assert exc.value.reason == RefusalReason.GENERATION_MISMATCH.value

    def test_stale_quote_refused(self) -> None:
        prov = live_provenance(event_at=NOW - timedelta(seconds=30))
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(prov)
        assert exc.value.reason == RefusalReason.STALE_QUOTE.value

    def test_fresh_local_receipt_does_not_rescue_stale_provider_data(self) -> None:
        """The exact trap: delayed data delivered a moment ago."""
        prov = MarketDataProvenance(
            requested_type=1,
            subscription_generation=uuid4(),
            subscribed_at=NOW,
            reported_type=1,
            callback_received=True,
            last_provider_event_at=NOW - timedelta(minutes=15),
            last_local_receive_at=NOW,
        )
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(prov)
        assert exc.value.reason == RefusalReason.STALE_QUOTE.value

    def test_missing_provider_timestamp_refused(self) -> None:
        prov = MarketDataProvenance(
            requested_type=1,
            subscription_generation=uuid4(),
            subscribed_at=NOW,
            reported_type=1,
            callback_received=True,
            last_local_receive_at=NOW,
        )
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(prov)
        assert exc.value.reason == RefusalReason.NO_PROVIDER_TIMESTAMP.value

    def test_refusal_is_catchable_as_a_refusal(self) -> None:
        with pytest.raises(RefusedError):
            self._call(live_provenance(reported=MarketDataType.DELAYED))


# ===========================================================================
# Whole-structure provenance
# ===========================================================================


class TestUniformProvenance:
    def _snapshot(self, *, underlying_reported=1, option_reported=1, delta="0.16"):
        u_gen, a_gen, b_gen = uuid4(), uuid4(), uuid4()
        underlying = UnderlyingQuote(
            symbol="SPY",
            provenance=live_provenance(u_gen, reported=underlying_reported),
            bid=D("499.90"),
            ask=D("500.10"),
        )
        legs = (
            OptionQuote(
                con_id=1001,
                provenance=live_provenance(a_gen, reported=option_reported),
                bid=D("1.40"),
                ask=D("1.60"),
                greeks=greeks(a_gen, delta=delta),
            ),
            OptionQuote(
                con_id=1002,
                provenance=live_provenance(b_gen, reported=option_reported),
                bid=D("0.40"),
                ask=D("0.60"),
                greeks=greeks(b_gen, delta="0.10"),
            ),
        )
        generations = {"underlying": u_gen, "1001": a_gen, "1002": b_gen}
        return underlying, legs, generations

    def _call(self, underlying, legs, generations):
        require_uniform_live_provenance(
            underlying=underlying,
            legs=legs,
            decision_time=NOW,
            maximum_age=MAX_AGE,
            active_generations=generations,
        )

    def test_all_live_passes(self) -> None:
        self._call(*self._snapshot())

    def test_underlying_live_but_options_delayed_refused(self) -> None:
        """IBKR computes greeks from the underlying; the mismatch produces
        deltas that look fine and describe a different market."""
        underlying, legs, gens = self._snapshot(option_reported=3)
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(underlying, legs, gens)
        assert exc.value.reason == "OPTIONS_REALTIME_DATA_REQUIRED"

    def test_options_live_but_underlying_delayed_refused(self) -> None:
        underlying, legs, gens = self._snapshot(underlying_reported=3)
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(underlying, legs, gens)
        assert exc.value.reason == "OPTIONS_REALTIME_DATA_REQUIRED"

    def test_underlying_callback_does_not_establish_option_provenance(self) -> None:
        """A live underlying proves nothing about the option subscription."""
        u_gen, a_gen = uuid4(), uuid4()
        underlying = UnderlyingQuote(
            symbol="SPY", provenance=live_provenance(u_gen), bid=D("499"), ask=D("501")
        )
        silent_option = OptionQuote(
            con_id=1001,
            provenance=MarketDataProvenance(
                requested_type=1,
                subscription_generation=a_gen,
                subscribed_at=NOW,
            ),
            greeks=greeks(a_gen),
        )
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(
                underlying, (silent_option,), {"underlying": u_gen, "1001": a_gen}
            )
        assert exc.value.reason == RefusalReason.NO_DATA_TYPE_CALLBACK.value

    def test_missing_greeks_refused(self) -> None:
        underlying, legs, gens = self._snapshot()
        stripped = (legs[0], OptionQuote(con_id=1002, provenance=legs[1].provenance))
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(underlying, stripped, gens)
        assert exc.value.reason == RefusalReason.GREEKS_MISSING.value

    def test_greeks_present_without_delta_refused(self) -> None:
        underlying, legs, gens = self._snapshot(delta=None)
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(underlying, legs, gens)
        assert exc.value.reason == RefusalReason.DELTA_INVALID.value

    def test_greeks_from_an_earlier_generation_refused(self) -> None:
        """Generation A greeks cannot satisfy a generation B request."""
        u_gen, a_gen = uuid4(), uuid4()
        underlying = UnderlyingQuote(
            symbol="SPY", provenance=live_provenance(u_gen), bid=D("499"), ask=D("501")
        )
        leg = OptionQuote(
            con_id=1001,
            provenance=live_provenance(a_gen),
            bid=D("1.40"),
            ask=D("1.60"),
            greeks=greeks(uuid4()),  # stamped with a generation that is not a_gen
        )
        with pytest.raises(MarketDataRefusedError) as exc:
            self._call(underlying, (leg,), {"underlying": u_gen, "1001": a_gen})
        assert exc.value.reason == RefusalReason.GENERATION_MISMATCH.value

    def test_empty_leg_set_refused(self) -> None:
        underlying, _, gens = self._snapshot()
        with pytest.raises(MarketDataRefusedError):
            self._call(underlying, (), gens)


# ===========================================================================
# Derived quote arithmetic
# ===========================================================================


class TestQuoteArithmetic:
    def test_mid_and_spread(self) -> None:
        quote = OptionQuote(
            con_id=1, provenance=live_provenance(), bid=D("1.40"), ask=D("1.60")
        )
        assert quote.mid == D("1.50")
        assert quote.spread == D("0.20")

    def test_spread_fraction(self) -> None:
        quote = OptionQuote(
            con_id=1, provenance=live_provenance(), bid=D("1.40"), ask=D("1.60")
        )
        assert quote.spread_fraction is not None
        assert quote.spread_fraction.quantize(D("0.0001")) == D("0.1333")

    def test_missing_side_yields_none_not_zero(self) -> None:
        quote = OptionQuote(con_id=1, provenance=live_provenance(), bid=D("1.40"))
        assert quote.mid is None
        assert quote.spread is None
        assert quote.spread_fraction is None

    def test_nonpositive_mid_gives_no_spread_fraction(self) -> None:
        """A zero mid would otherwise produce a division error or an
        enormous ratio that reads as a merely-wide market."""
        quote = OptionQuote(
            con_id=1, provenance=live_provenance(), bid=D("0"), ask=D("0")
        )
        assert quote.spread_fraction is None
