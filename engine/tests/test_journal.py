"""The durable order journal.

The behaviour under test is mostly what the journal *refuses* to do quietly:
lose a record, rotate away history, or report success for a write that failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.errors import JournalError
from engine.journal import OrderJournal


class TestWriting:
    def test_a_record_round_trips(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "orders.jsonl")
        journal.record("order_placed", symbol="SPY", quantity=1, side="BUY")
        records = journal.records()
        assert len(records) == 1
        assert records[0]["event"] == "order_placed"
        assert records[0]["symbol"] == "SPY"
        assert records[0]["ts"].endswith("Z")

    def test_records_append_and_never_overwrite(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "orders.jsonl")
        for index in range(5):
            journal.record("order_placed", n=index)
        assert [r["n"] for r in journal.records()] == [0, 1, 2, 3, 4]

    def test_a_second_instance_appends_rather_than_truncating(self, tmp_path: Path) -> None:
        path = tmp_path / "orders.jsonl"
        OrderJournal(path).record("order_placed", n=1)
        OrderJournal(path).record("order_placed", n=2)
        assert [r["n"] for r in OrderJournal(path).records()] == [1, 2]

    def test_every_line_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "orders.jsonl"
        journal = OrderJournal(path)
        journal.record("order_placed", note="has \"quotes\" and \n newlines")
        for line in path.read_text(encoding="utf-8").splitlines():
            json.loads(line)

    def test_none_valued_fields_are_dropped_not_stored_as_null(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "orders.jsonl")
        journal.record("preview", symbol="SPY", limit_price=None)
        assert "limit_price" not in journal.records()[0]

    def test_the_parent_directory_is_created(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "deep" / "nested" / "orders.jsonl")
        journal.record("preflight")
        assert journal.path.is_file()


class TestFailureIsFatal:
    """The inversion of collab-kit's EventLog: a failed write must not be silent."""

    def test_an_unwritable_path_raises_rather_than_returning(self, tmp_path: Path) -> None:
        # A directory where the file should be: open() cannot succeed.
        blocked = tmp_path / "orders.jsonl"
        blocked.mkdir()
        with pytest.raises(JournalError) as caught:
            OrderJournal(blocked).record("order_placed", symbol="SPY")
        assert "cannot write" in str(caught.value)

    def test_an_unserializable_record_raises(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "orders.jsonl")

        class Exploding:
            def __str__(self) -> str:
                raise RuntimeError("nope")

        with pytest.raises(JournalError):
            journal.record("order_placed", payload=Exploding())

    def test_preflight_surfaces_an_unwritable_journal_before_trading(
        self, tmp_path: Path
    ) -> None:
        blocked = tmp_path / "orders.jsonl"
        blocked.mkdir()
        with pytest.raises(JournalError):
            OrderJournal(blocked).preflight()


class TestReading:
    def test_a_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert OrderJournal(tmp_path / "nope.jsonl").records() == []

    def test_a_torn_final_line_does_not_lose_earlier_records(self, tmp_path: Path) -> None:
        path = tmp_path / "orders.jsonl"
        journal = OrderJournal(path)
        journal.record("order_placed", n=1)
        journal.record("order_placed", n=2)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"event": "order_placed", "n": 3')  # killed mid-write
        assert [r["n"] for r in journal.records()] == [1, 2]

    def test_orders_today_counts_only_placed_orders(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "orders.jsonl")
        journal.record("preview", symbol="SPY")
        journal.record("order_placed", symbol="SPY")
        journal.record("order_result", symbol="SPY")
        journal.record("refused", symbol="SPY")
        assert journal.orders_today() == 1

    def test_orders_from_another_day_do_not_count(self, tmp_path: Path) -> None:
        path = tmp_path / "orders.jsonl"
        path.write_text(
            json.dumps({"v": 1, "ts": "2020-01-01T00:00:00Z", "event": "order_placed"}) + "\n",
            encoding="utf-8",
        )
        assert OrderJournal(path).orders_today() == 0

    def test_tail_returns_the_most_recent(self, tmp_path: Path) -> None:
        journal = OrderJournal(tmp_path / "orders.jsonl")
        for index in range(10):
            journal.record("order_placed", n=index)
        assert [r["n"] for r in journal.tail(3)] == [7, 8, 9]


class TestNoRotation:
    def test_the_journal_is_never_rotated_or_truncated(self, tmp_path: Path) -> None:
        # collab-kit's EventLog rotates at 8MB and discards the older
        # generation. A trading record must not: assert no rotation machinery
        # exists and that a large file keeps every record.
        journal = OrderJournal(tmp_path / "orders.jsonl")
        assert not hasattr(journal, "rotate_bytes")
        assert not hasattr(journal, "_rotate_if_needed")
        for index in range(500):
            journal.record("order_placed", n=index, padding="x" * 200)
        assert len(journal.records()) == 500
        assert not list(tmp_path.glob("orders.jsonl.1"))
