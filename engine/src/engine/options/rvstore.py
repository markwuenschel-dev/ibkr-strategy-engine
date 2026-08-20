"""A per-symbol cache of daily underlying-close observations, for realized vol.

Same rationale, same file shape, and the same pacing motive as
:class:`~engine.options.ivstore.IVStore`: eighty symbols times sixty daily
TRADES bars is eighty more historical-data requests on top of the IV pull
IVStore already caches, against the same IBKR pacing window. Without a cache,
the realized-vol refresh this module backs would double every scan's
historical-request cost forever; with one, a second scan the same day costs
zero requests for the price series too.

The store keeps the *raw* :class:`~engine.options.realized_vol.PriceObservation`
series, not the derived metric, for the same reason IVStore does: realized
vol, and any future statistic over the same closes, is recomputed from inputs
rather than trusted from a cache of conclusions.

Format: one JSONL file per symbol under ``<state_dir>/universe/rv/``. The
first line is a meta record; each further line is one observation. Rewritten
whole and atomically on refresh (temp + ``os.replace``).
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
from .realized_vol import PriceObservation, SOURCE_IBKR_TRADES

__all__ = ["CachedPriceSeries", "RVStore", "RVSTORE_VERSION"]

RVSTORE_VERSION = "rvstore/1"

#: Same cadence as IVStore -- both series update once per session.
DEFAULT_TTL = dt.timedelta(hours=20)


@dataclass(frozen=True)
class CachedPriceSeries:
    """One symbol's cached daily closes, with the provenance to judge them."""

    symbol: str
    observations: tuple[PriceObservation, ...]
    fetched_at: dt.datetime | None
    source: str
    envelope: ObservationEnvelope | None = None

    @property
    def last_observation(self) -> dt.date | None:
        return self.observations[-1].on if self.observations else None


class RVStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, symbol: str) -> Path:
        clean = "".join(c for c in symbol.strip().upper() if c.isalnum() or c in "._-")
        if not clean:
            raise ValueError(f"not a cacheable symbol: {symbol!r}")
        return self.root / f"{clean}.jsonl"

    # -- reading -------------------------------------------------------

    def read(self, symbol: str) -> CachedPriceSeries:
        """The cached series, empty when absent or unreadable.

        A corrupt line degrades the series rather than bricking it -- the
        same contract as IVStore: a scanner that cannot read one cache file
        must not lose the other seventy-nine.
        """
        path = self._path(symbol)
        observations: list[PriceObservation] = []
        fetched_at: dt.datetime | None = None
        source = SOURCE_IBKR_TRADES
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return CachedPriceSeries(
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
                close = Decimal(str(record.get("close", "")))
            except (ValueError, InvalidOperation):
                continue
            if close > 0:
                observations.append(PriceObservation(on=on, close=close))
        observations.sort(key=lambda o: o.on)
        return CachedPriceSeries(
            symbol=symbol.strip().upper(),
            observations=tuple(observations),
            fetched_at=fetched_at,
            source=source,
            envelope=envelope,
        )

    def fresh(
        self,
        symbol: str,
        *,
        today: dt.date,
        now: dt.datetime,
        previous_session: dt.date | None = None,
    ) -> bool:
        """Fresh enough to skip the broker -- identical rule to
        :meth:`IVStore.fresh`: unexpired envelope, fetched this session, and
        reaches the previous trading session."""
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

    # -- writing -------------------------------------------------------

    def write(
        self,
        symbol: str,
        observations: list[PriceObservation] | tuple[PriceObservation, ...],
        *,
        fetched_at: dt.datetime,
        source: str = SOURCE_IBKR_TRADES,
        ttl: dt.timedelta = DEFAULT_TTL,
        configuration_version: str = RVSTORE_VERSION,
    ) -> Path:
        """Atomic whole-file rewrite. A crash mid-write leaves the old file."""
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
                        "close": str(observation.close),
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
