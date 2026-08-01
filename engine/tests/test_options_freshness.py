"""Freshness classes: session metadata reuses, slow observations expire,
perishables never outlive their bounds.

The 2026-08-01 audit's core sentence: "second scan of a day = zero broker
requests" is not acceptable for perishable market observations. These tests
pin what each class may and may not skip.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import uuid4

import pytest

from engine.options.freshness import (
    FreshnessClass,
    FreshnessPolicy,
    ObservationEnvelope,
    SessionMetadataStore,
    SymbolSessionMetadata,
)
from engine.options.ivrank import IVObservation
from engine.options.ivstore import IVStore

D = Decimal
NOW = dt.datetime(2026, 8, 3, 13, 0, tzinfo=dt.timezone.utc)  # a Monday
TODAY = NOW.date()


def envelope(
    *,
    freshness_class: FreshnessClass,
    observed_at: dt.datetime = NOW,
    ttl: dt.timedelta = dt.timedelta(hours=4),
    session_date: dt.date | None = None,
) -> ObservationEnvelope:
    return ObservationEnvelope(
        symbol="SPY",
        session_date=session_date or observed_at.date(),
        observed_at=observed_at,
        expires_at=observed_at + ttl,
        source="test",
        freshness_class=freshness_class,
        configuration_version="test/1",
        market_data_type=1,
        subscription_generation=uuid4(),
    )


class TestEnvelopeSemantics:
    def test_session_metadata_lives_and_dies_with_its_session(self) -> None:
        e = envelope(freshness_class=FreshnessClass.SESSION_METADATA)
        assert e.fresh(now=NOW + dt.timedelta(hours=23), session_date=TODAY)
        assert not e.fresh(
            now=NOW, session_date=TODAY + dt.timedelta(days=1)
        ), "yesterday's contract catalog is not today's"

    def test_slow_observation_honours_its_expiry(self) -> None:
        e = envelope(freshness_class=FreshnessClass.SLOW_OBSERVATION)
        assert e.fresh(now=NOW + dt.timedelta(hours=3), session_date=TODAY)
        assert not e.fresh(now=NOW + dt.timedelta(hours=5), session_date=TODAY)

    def test_perishable_never_outlives_its_session_whatever_its_ttl(self) -> None:
        """A generous TTL cannot resurrect yesterday's quote."""
        e = envelope(
            freshness_class=FreshnessClass.PERISHABLE,
            ttl=dt.timedelta(days=7),
        )
        tomorrow = TODAY + dt.timedelta(days=1)
        assert not e.fresh(
            now=NOW + dt.timedelta(days=1), session_date=tomorrow
        )

    def test_the_envelope_round_trips_every_binding_field(self) -> None:
        e = envelope(freshness_class=FreshnessClass.SLOW_OBSERVATION)
        again = ObservationEnvelope.from_record(e.to_record())
        assert again == e

    def test_inverted_instants_refuse_construction(self) -> None:
        with pytest.raises(ValueError):
            ObservationEnvelope(
                symbol="SPY",
                session_date=TODAY,
                observed_at=NOW,
                expires_at=NOW - dt.timedelta(seconds=1),
                source="test",
                freshness_class=FreshnessClass.SLOW_OBSERVATION,
                configuration_version="test/1",
            )


def metadata(*, session_date: dt.date = TODAY) -> SymbolSessionMetadata:
    observed = dt.datetime.combine(session_date, dt.time(13, 0), dt.timezone.utc)
    return SymbolSessionMetadata(
        envelope=ObservationEnvelope(
            symbol="SPY",
            session_date=session_date,
            observed_at=observed,
            expires_at=observed + dt.timedelta(hours=23),
            source="IBKR:contract-details",
            freshness_class=FreshnessClass.SESSION_METADATA,
            configuration_version="test/1",
        ),
        con_id=756733,
        expirations=("20260918", "20261016"),
        multiplier=100,
        standard=True,
        sector="BROAD_MARKET",
        correlation_group="US_LARGE_CAP",
    )


class TestSessionMetadataStore:
    def test_same_day_contract_metadata_is_reused(self, tmp_path) -> None:
        """Mandated: the second read within a session costs no fetch --
        ``read`` returning the record IS the reuse decision."""
        store = SessionMetadataStore(tmp_path / "meta")
        store.write(metadata())
        cached = store.read("SPY", session_date=TODAY, now=NOW)
        assert cached is not None
        assert cached.con_id == 756733
        assert cached.expirations == ("20260918", "20261016")
        assert cached.standard is True

    def test_yesterdays_metadata_is_not_todays(self, tmp_path) -> None:
        store = SessionMetadataStore(tmp_path / "meta")
        store.write(metadata(session_date=TODAY - dt.timedelta(days=3)))
        assert store.read("SPY", session_date=TODAY, now=NOW) is None

    def test_an_unknown_symbol_reads_none(self, tmp_path) -> None:
        store = SessionMetadataStore(tmp_path / "meta")
        store.write(metadata())
        assert store.read("QQQ", session_date=TODAY, now=NOW) is None


def observations(count: int = 70, *, last: dt.date) -> list[IVObservation]:
    return [
        IVObservation(
            on=last - dt.timedelta(days=count - 1 - i),
            implied_volatility=D("0.15") + D(i) / 1000,
        )
        for i in range(count)
    ]


class TestSlowObservationTTL:
    FRIDAY = dt.date(2026, 7, 31)

    def test_fresh_slow_observations_are_reused(self, tmp_path) -> None:
        """Mandated: an unexpired same-session series skips the broker."""
        store = IVStore(tmp_path / "iv")
        store.write("SPY", observations(last=self.FRIDAY), fetched_at=NOW)
        assert store.fresh("SPY", today=TODAY, now=NOW + dt.timedelta(hours=2))

    def test_expired_iv_observations_refresh(self, tmp_path) -> None:
        """Mandated: a TTL-expired series refreshes even within its session."""
        store = IVStore(tmp_path / "iv")
        store.write(
            "SPY",
            observations(last=self.FRIDAY),
            fetched_at=NOW,
            ttl=dt.timedelta(hours=1),
        )
        assert store.fresh("SPY", today=TODAY, now=NOW + dt.timedelta(minutes=30))
        assert not store.fresh("SPY", today=TODAY, now=NOW + dt.timedelta(hours=2))

    def test_a_legacy_record_without_an_envelope_is_stale(self, tmp_path) -> None:
        """Provenance that cannot be stated cannot be reused."""
        store = IVStore(tmp_path / "iv")
        path = store.write("SPY", observations(last=self.FRIDAY), fetched_at=NOW)
        lines = path.read_text(encoding="utf-8").splitlines()
        import json

        meta = json.loads(lines[0])
        del meta["meta"]["envelope"]
        lines[0] = json.dumps(meta, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert not store.fresh("SPY", today=TODAY, now=NOW)

    def test_the_freshness_policy_reads_env_ttls(self) -> None:
        policy = FreshnessPolicy.from_env(
            {"IBKR_OPTIONS_FRESHNESS_IV_HISTORY_TTL_SECONDS": "3600"}
        )
        assert policy.iv_history_ttl == dt.timedelta(hours=1)
        assert policy.open_interest_ttl == dt.timedelta(hours=4)
