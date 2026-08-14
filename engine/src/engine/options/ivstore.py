"""A per-symbol cache of daily implied-volatility observations.

Ninety symbols times one year of history is ninety historical-data requests,
and IBKR's pacing window allows roughly sixty per ten minutes. Without a
cache, every universe scan re-spends the whole budget on data that changed by
exactly one bar since yesterday. With one, a second scan the same day costs
zero requests, and the daily refresh is one request per symbol.

The store keeps the *raw* :class:`~engine.options.ivrank.IVObservation`
series -- which its own docstring says should be persisted separately from
the derived metric -- so IV Rank, IV percentile, and any future statistic are
recomputed from inputs rather than trusted from a cache of conclusions.

Format: one JSONL file per symbol under ``<state_dir>/universe/iv/``. The
first line is a meta record (source label, fetch instant); each further line
is one observation. Rewritten whole and atomically on refresh (temp +
``os.replace``): pacing cost is per-request, not per-bar, so an incremental
merge would buy nothing and lose self-healing.

Freshness is a *decision input*, not a hidden policy: :meth:`IVStore.fresh`
answers for a given ``today``, and the caller (the universe scanner) decides
what staleness means for its run. A stale cache is still returned by
:meth:`read` -- degraded IV data with its age stated beats no data, and the
metric's ``degraded_reason`` machinery already knows how to say so.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .freshness import FreshnessClass, ObservationEnvelope
from .ivrank import IVObservation, SOURCE_IBKR_OPTION_IV

__all__ = ["CachedSeries", "IVStore", "IVSTORE_VERSION"]

IVSTORE_VERSION = "ivstore/1"

#: The IV input series is a SLOW_OBSERVATION (2026-08-01 audit): it may be
#: reused while its envelope is unexpired, and it must carry that envelope.
DEFAULT_TTL = dt.timedelta(hours=20)


@dataclass(frozen=True)
class CachedSeries:
    """One symbol's cached observations, with the provenance to judge them."""

    symbol: str
    observations: tuple[IVObservation, ...]
    fetched_at: dt.datetime | None
    source: str
    envelope: ObservationEnvelope | None = None
    catalog_version: str | None = None

    @property
    def last_observation(self) -> dt.date | None:
        return self.observations[-1].on if self.observations else None


class IVStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, symbol: str) -> Path:
        clean = "".join(c for c in symbol.strip().upper() if c.isalnum() or c in "._-")
        if not clean:
            raise ValueError(f"not a cacheable symbol: {symbol!r}")
        return self.root / f"{clean}.jsonl"

    # -- reading -----------------------------------------------------------

    def read(self, symbol: str) -> CachedSeries:
        """The cached series, empty when absent or unreadable.

        A corrupt line degrades the series rather than bricking it -- the
        same contract as the position store, and for the same reason: a
        scanner that cannot read one cache file must not lose the other 89.
        """
        path = self._path(symbol)
        observations: list[IVObservation] = []
        fetched_at: dt.datetime | None = None
        source = SOURCE_IBKR_OPTION_IV
        catalog_version: str | None = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return CachedSeries(
                symbol=symbol.strip().upper(),
                observations=(),
                fetched_at=None,
                source=source,
            )
        envelope: ObservationEnvelope | None = None
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            meta = record.get("meta")
            if isinstance(meta, dict):
                source = str(meta.get("source", source))
                if meta.get("catalog_version") is not None:
                    catalog_version = str(meta["catalog_version"])
                raw_fetched = str(meta.get("fetched_at", ""))
                try:
                    fetched_at = dt.datetime.fromisoformat(raw_fetched)
                except ValueError:
                    fetched_at = None
                raw_envelope = meta.get("envelope")
                if isinstance(raw_envelope, dict):
                    with contextlib.suppress(KeyError, ValueError, TypeError):
                        candidate = ObservationEnvelope.from_record(raw_envelope)
                        if candidate.symbol == symbol.strip().upper():
                            envelope = candidate
                continue
            try:
                on = dt.date.fromisoformat(str(record.get("on", "")))
                implied = Decimal(str(record.get("iv", "")))
            except (ValueError, InvalidOperation):
                continue
            if implied > 0:
                observations.append(IVObservation(on=on, implied_volatility=implied))
        observations.sort(key=lambda o: o.on)
        return CachedSeries(
            symbol=symbol.strip().upper(),
            observations=tuple(observations),
            fetched_at=fetched_at,
            source=source,
            envelope=envelope,
            catalog_version=catalog_version,
        )

    def fresh(
        self,
        symbol: str,
        *,
        today: dt.date,
        now: dt.datetime,
        previous_session: dt.date | None = None,
    ) -> bool:
        """Fresh enough to skip the broker, on three conditions at once:
        the record's envelope is unexpired (SLOW_OBSERVATION TTL), it was
        fetched this session, and the series reaches the previous trading
        session. A legacy record without an envelope is stale by definition
        -- provenance that cannot be stated cannot be reused.

        ``previous_session`` defaults to a weekend-aware yesterday. Holidays
        make that estimate wrong by a day, in the *conservative* direction:
        the store re-fetches when it did not strictly need to, spending one
        request rather than trading on a stale rank.
        """
        cached = self.read(symbol)
        if cached.envelope is None or not cached.observations:
            return False
        if not cached.envelope.fresh(now=now, session_date=today):
            return False
        if cached.envelope.session_date != today:
            return False
        if previous_session is None:
            previous_session = _previous_weekday(today)
        last = cached.last_observation
        return last is not None and last >= previous_session

    # -- writing -----------------------------------------------------------

    def write(
        self,
        symbol: str,
        observations: list[IVObservation] | tuple[IVObservation, ...],
        *,
        fetched_at: dt.datetime,
        source: str = SOURCE_IBKR_OPTION_IV,
        ttl: dt.timedelta = DEFAULT_TTL,
        configuration_version: str = IVSTORE_VERSION,
        catalog_version: str | None = None,
    ) -> Path:
        """Atomic whole-file rewrite. A crash mid-write leaves the old file.

        Every write mints the full SLOW_OBSERVATION envelope: session date,
        observed/expiry instants, source, a fetch-scoped generation (an
        historical pull has no live subscription to inherit one from), and
        the configuration version. ``market_data_type`` stays ``None`` --
        historical bars produce no type callback, and absence is recorded
        rather than invented.
        """
        envelope = ObservationEnvelope(
            symbol=symbol.strip().upper(),
            session_date=fetched_at.date(),
            observed_at=fetched_at,
            expires_at=fetched_at + ttl,
            source=source,
            freshness_class=FreshnessClass.SLOW_OBSERVATION,
            configuration_version=configuration_version,
            market_data_type=None,
            subscription_generation=uuid4(),
        )
        path = self._path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "meta": {
                        "source": source,
                        "fetched_at": fetched_at.isoformat(),
                        "envelope": envelope.to_record(),
                        "catalog_version": catalog_version,
                    }
                },
                sort_keys=True,
            )
        ]
        for observation in sorted(observations, key=lambda o: o.on):
            lines.append(
                json.dumps(
                    {
                        "on": observation.on.isoformat(),
                        "iv": str(observation.implied_volatility),
                    },
                    sort_keys=True,
                )
            )
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
            os.replace(temp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
        return path


def _previous_weekday(today: dt.date) -> dt.date:
    day = today - dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day
