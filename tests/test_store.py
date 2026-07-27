"""The handoff state machine: the directory a file sits in *is* its state.

Every assertion here is about a file physically being in one directory and not
another, because that is the only state the system has. The concurrency test at
the bottom pins the property the whole design rests on: when N agents race to
claim the same handoff, exactly one may win.
"""

from __future__ import annotations

import os
import sys
import threading

from collabkit import frontmatter, ids
from collabkit.errors import CollabKitError, ConflictError, NotFoundError, ValidationError
from collabkit.paths import STATES
from collabkit.watch import WatchTarget, Watcher

from tests.support import IsolatedHomeTestCase


class HandoffLifecycleTests(IsolatedHomeTestCase):
    """create -> claim -> done -> archive, tracked by where the file lives."""

    def setUp(self):
        super().setUp()
        self.paths = self.make_collab("demo")
        self.store = self.make_store("demo")

    def test_a_new_handoff_lands_in_pending_and_nowhere_else(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Look at this")
        self.assertEqual(handoff.status, "pending")
        self.assertTrue((self.paths.pending / f"{handoff.id}.md").is_file())
        self.assertEqual(self.store.counts(), {"pending": 1, "claimed": 0, "done": 0, "archive": 0})

    def test_the_file_moves_between_directories_at_each_transition(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Move me")
        handoff_id = handoff.id

        claimed = self.store.claim(handoff_id, by="reviewer")
        self.assertEqual(claimed.status, "claimed")
        self.assertFalse((self.paths.pending / f"{handoff_id}.md").exists())
        self.assertTrue((self.paths.claimed / f"{handoff_id}.md").is_file())
        self.assertEqual(claimed.claimed_by, "reviewer")
        self.assertTrue(claimed.claimed_at)

        done = self.store.complete(handoff_id, note="all good")
        self.assertEqual(done.status, "done")
        self.assertFalse((self.paths.claimed / f"{handoff_id}.md").exists())
        self.assertTrue((self.paths.done / f"{handoff_id}.md").is_file())
        self.assertEqual(done.note, "all good")

        archived = self.store.archive(handoff_id)
        self.assertEqual(archived.status, "archive")
        self.assertFalse((self.paths.done / f"{handoff_id}.md").exists())

        year, month = ids.archive_partition(handoff_id)
        self.assertTrue((self.paths.archive / year / month / f"{handoff_id}.md").is_file())

    def test_the_archive_is_partitioned_by_year_and_month(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Partition me")
        self.store.complete(handoff.id)
        archived = self.store.archive(handoff.id)

        year, month = ids.archive_partition(handoff.id)
        self.assertRegex(year, r"^\d{4}$")
        self.assertRegex(month, r"^\d{2}$")
        relative = archived.path.relative_to(self.paths.archive)
        self.assertEqual(relative.parts[:2], (year, month))

    def test_pending_may_go_straight_to_done_without_being_claimed(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Shortcut")
        done = self.store.complete(handoff.id, by="reviewer")
        self.assertEqual(done.status, "done")
        self.assertTrue((self.paths.done / f"{handoff.id}.md").is_file())

    def test_creating_without_a_title_or_recipient_is_refused(self):
        with self.assertRaises(ValidationError):
            self.store.create(to="reviewer", sender="builder", title="   ")
        with self.assertRaises(ValidationError):
            self.store.create(to="", sender="builder", title="No recipient")

    def test_archive_all_done_sweeps_every_finished_handoff(self):
        finished = []
        for index in range(3):
            handoff = self.store.create(to="reviewer", sender="builder", title=f"Done {index}")
            self.store.complete(handoff.id)
            finished.append(handoff.id)
        still_open = self.store.create(to="reviewer", sender="builder", title="Open")

        archived = self.store.archive_all_done()

        self.assertEqual(sorted(item.id for item in archived), sorted(finished))
        self.assertEqual(self.store.counts()["done"], 0)
        self.assertEqual(self.store.counts()["archive"], 3)
        self.assertTrue((self.paths.pending / f"{still_open.id}.md").is_file())

    def test_listing_filters_by_recipient_priority_and_tag(self):
        self.store.create(to="reviewer", sender="builder", title="A", priority="high", tags=["auth"])
        self.store.create(to="builder", sender="reviewer", title="B", priority="low", tags=["docs"])

        self.assertEqual([h.title for h in self.store.list(("pending",), to="reviewer")], ["A"])
        self.assertEqual([h.title for h in self.store.list(("pending",), priority="low")], ["B"])
        self.assertEqual([h.title for h in self.store.list(("pending",), tag="auth")], ["A"])
        self.assertEqual(len(self.store.list(("pending",))), 2)

    def test_listing_sorts_most_urgent_first_then_oldest_first(self):
        low = self.store.create(to="reviewer", sender="builder", title="low", priority="low")
        urgent = self.store.create(to="reviewer", sender="builder", title="urgent", priority="urgent")
        normal = self.store.create(to="reviewer", sender="builder", title="normal")

        found = [h.title for h in self.store.list(("pending",))]
        self.assertEqual(found[0], "urgent")
        self.assertEqual(found[-1], "low")
        self.assertEqual(sorted(found), sorted([low.title, urgent.title, normal.title]))


class HandoffResolutionTests(IsolatedHomeTestCase):
    """``find`` accepts an exact id or an unambiguous prefix -- never a guess."""

    def setUp(self):
        super().setUp()
        self.store = self.make_store("demo")

    def test_an_exact_id_resolves_in_every_state(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Findable")
        for step in (None, "claim", "complete", "archive"):
            with self.subTest(step=step or "pending"):
                if step:
                    getattr(self.store, step)(handoff.id)
                self.assertEqual(self.store.find(handoff.id).id, handoff.id)

    def test_an_unambiguous_prefix_resolves(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Prefixed")
        # The trailing slug fragment is unique here; the timestamp is not.
        prefix = handoff.id[:-4]
        self.assertEqual(self.store.find(prefix).id, handoff.id)

    def test_an_ambiguous_prefix_raises_conflict_rather_than_picking_one(self):
        first = self.store.create(to="reviewer", sender="builder", title="Same start")
        second = self.store.create(to="reviewer", sender="builder", title="Same start too")
        shared = os.path.commonprefix([first.id, second.id])
        self.assertTrue(shared, "the two ids should share a timestamp prefix")

        with self.assertRaises(ConflictError):
            self.store.find(shared)

    def test_an_exact_id_is_never_treated_as_a_prefix(self):
        first = self.store.create(to="reviewer", sender="builder", title="Exact")
        # A second handoff whose id starts with the first one's would make the
        # first ambiguous if exact matching did not win.
        longer = first.id + "-extra"
        (self.store.paths.pending / f"{longer}.md").write_text(
            frontmatter.dumps({"id": longer, "to": "reviewer", "from": "builder", "title": "Longer"}),
            encoding="utf-8",
        )
        self.assertEqual(self.store.find(first.id).id, first.id)

    def test_an_unknown_id_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.find("20990101T000000Z-ffffff-nope")

    def test_an_empty_id_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            self.store.find("   ")


class HandoffTransitionTests(IsolatedHomeTestCase):
    """Only forward, only to an adjacent-or-later state."""

    def setUp(self):
        super().setUp()
        self.store = self.make_store("demo")

    def test_claiming_an_already_claimed_handoff_is_a_conflict(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Once only")
        self.store.claim(handoff.id, by="reviewer")
        with self.assertRaises(ConflictError):
            self.store.claim(handoff.id, by="builder")

    def test_claiming_a_finished_handoff_is_a_conflict(self):
        for step in ("complete", "archive"):
            with self.subTest(state=step):
                handoff = self.store.create(to="reviewer", sender="builder", title=f"Gone {step}")
                self.store.complete(handoff.id)
                if step == "archive":
                    self.store.archive(handoff.id)
                with self.assertRaises(ConflictError):
                    self.store.claim(handoff.id)

    def test_completing_twice_is_a_conflict(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Done once")
        self.store.complete(handoff.id)
        with self.assertRaises(ConflictError):
            self.store.complete(handoff.id)

    def test_archiving_a_pending_handoff_is_refused(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Not done yet")
        with self.assertRaises(ConflictError):
            self.store.archive(handoff.id)
        self.assertTrue((self.store.paths.pending / f"{handoff.id}.md").is_file())

    def test_archiving_a_claimed_handoff_is_refused(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="In progress")
        self.store.claim(handoff.id)
        with self.assertRaises(ConflictError):
            self.store.archive(handoff.id)

    def test_archiving_twice_is_a_conflict(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Archived once")
        self.store.complete(handoff.id)
        self.store.archive(handoff.id)
        with self.assertRaises(ConflictError):
            self.store.archive(handoff.id)

    def test_an_archived_handoff_cannot_be_reopened(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Sealed")
        self.store.complete(handoff.id)
        self.store.archive(handoff.id)
        with self.assertRaises(ConflictError):
            self.store.complete(handoff.id)


class HandoffReplyTests(IsolatedHomeTestCase):
    """A reply is a new handoff back to the sender, on the parent's thread."""

    def setUp(self):
        super().setUp()
        self.store = self.make_store("demo")

    def test_a_reply_threads_on_the_parent_id_and_goes_back_to_its_sender(self):
        parent = self.store.create(to="reviewer", sender="builder", title="Please review")
        reply, closed = self.store.reply(parent.id, title="Reviewed", sender="reviewer")

        self.assertEqual(reply.thread, parent.id)
        self.assertEqual(reply.to, "builder")
        self.assertEqual(reply.sender, "reviewer")
        self.assertIsNotNone(closed)
        self.assertEqual(closed.status, "done")

    def test_a_reply_to_a_reply_stays_on_the_original_thread(self):
        parent = self.store.create(to="reviewer", sender="builder", title="Turn one")
        first, _ = self.store.reply(parent.id, title="Turn two", sender="reviewer")
        second, _ = self.store.reply(first.id, title="Turn three", sender="builder")

        # A five-turn exchange must share one thread id, not form a chain that
        # has to be walked backwards.
        self.assertEqual(first.thread, parent.id)
        self.assertEqual(second.thread, parent.id)

    def test_keep_open_leaves_the_parent_pending(self):
        parent = self.store.create(to="reviewer", sender="builder", title="Stay open")
        reply, closed = self.store.reply(
            parent.id, title="Partial answer", sender="reviewer", close_parent=False
        )
        self.assertIsNone(closed)
        self.assertEqual(self.store.find(parent.id).status, "pending")
        self.assertEqual(reply.thread, parent.id)

    def test_a_reply_inherits_the_parent_priority_and_tags_by_default(self):
        parent = self.store.create(
            to="reviewer", sender="builder", title="Urgent thing",
            priority="urgent", tags=["auth", "security"],
        )
        reply, _ = self.store.reply(parent.id, title="Answer", sender="reviewer")
        self.assertEqual(reply.priority, "urgent")
        self.assertEqual(reply.tags, ["auth", "security"])


class HandoffRobustnessTests(IsolatedHomeTestCase):
    """Files this version did not write must not break this version."""

    def setUp(self):
        super().setUp()
        self.paths = self.make_collab("demo")
        self.store = self.make_store("demo")

    def test_unknown_frontmatter_keys_survive_a_claim_round_trip(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Future fields")
        path = self.paths.pending / f"{handoff.id}.md"
        meta, body = frontmatter.parse_file(path)
        meta["reviewers"] = ["grok", "claude"]
        meta["escalated"] = True
        meta["sla_hours"] = 4
        path.write_text(frontmatter.dumps(meta, body), encoding="utf-8")

        claimed = self.store.claim(handoff.id, by="reviewer")

        self.assertEqual(claimed.extra["reviewers"], ["grok", "claude"])
        after, _ = frontmatter.parse_file(claimed.path)
        self.assertEqual(after["reviewers"], ["grok", "claude"])
        self.assertIs(after["escalated"], True)
        self.assertEqual(after["sla_hours"], 4)
        # ...and the bookkeeping this version does understand still landed.
        self.assertEqual(after["status"], "claimed")
        self.assertEqual(after["claimed_by"], "reviewer")

    def test_a_corrupt_file_in_pending_is_skipped_rather_than_fatal(self):
        good = self.store.create(to="reviewer", sender="builder", title="Readable")
        corrupt = self.paths.pending / "20260101T000000Z-badbad-corrupt.md"
        corrupt.write_text("---\nto: reviewer\nnever closed\n", encoding="utf-8")

        listed = self.store.list(("pending",))

        self.assertEqual([item.id for item in listed], [good.id])
        self.assertEqual(self.store.counts()["pending"], 1)
        self.assertTrue(corrupt.is_file(), "a corrupt file must be left alone, not deleted")

    def test_atomic_write_temp_files_are_not_listed_as_handoffs(self):
        self.store.create(to="reviewer", sender="builder", title="Real one")
        (self.paths.pending / ".20260101T000000Z-aaaaaa-partial.md.tmp").write_text(
            "half written", encoding="utf-8"
        )
        (self.paths.pending / ".hidden.md").write_text("---\nid: x\n---\n", encoding="utf-8")
        self.assertEqual(len(self.store.list(("pending",))), 1)

    def test_state_dir_rejects_a_state_that_is_not_in_the_machine(self):
        self.assertEqual(set(STATES), {"pending", "claimed", "done", "archive"})
        with self.assertRaises(NotFoundError):
            self.paths.state_dir("in-review")

    def test_listing_an_unknown_state_name_yields_nothing_rather_than_raising(self):
        self.store.create(to="reviewer", sender="builder", title="Present")
        self.assertEqual(self.store.list(("nonsense",)), [])


class HandoffClaimRaceTests(IsolatedHomeTestCase):
    """The core safety property: a contested claim has exactly one winner."""

    THREADS = 8
    ROUNDS = 6

    def race_once(self, store, handoff_id):
        """Fire ``THREADS`` simultaneous claims. Returns (winners, failures)."""
        winners: list[str] = []
        failures: list[BaseException] = []
        guard = threading.Lock()
        gate = threading.Barrier(self.THREADS)

        def contend() -> None:
            gate.wait()
            try:
                claimed = store.claim(handoff_id, by="reviewer")
            except BaseException as exc:  # noqa: BLE001 - the type is the assertion
                with guard:
                    failures.append(exc)
            else:
                with guard:
                    winners.append(claimed.id)

        threads = [threading.Thread(target=contend) for _ in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        return winners, failures

    # Regression guard for the core safety property. os.rename alone is NOT
    # exclusive across concurrent threads on Windows (measured: 8 threads, 8
    # winners), so store._transition() decides the race with an O_EXCL FileLock
    # and renames underneath it. If someone removes that lock, this test is what
    # catches it.
    def test_exactly_one_thread_wins_a_contested_claim(self):
        store = self.make_store("demo")
        for round_index in range(self.ROUNDS):
            with self.subTest(round=round_index):
                handoff = store.create(
                    to="reviewer", sender="builder", title=f"Contested {round_index}"
                )
                winners, failures = self.race_once(store, handoff.id)

                self.assertEqual(
                    len(winners), 1,
                    f"{len(winners)} threads claimed the same handoff; exactly one may win",
                )
                self.assertEqual(len(failures), self.THREADS - 1)
                # Every loser must fail in a way the CLI maps to a real exit
                # code, not as an unhandled OS error.
                for failure in failures:
                    self.assertIsInstance(failure, CollabKitError, repr(failure))

    def test_a_contested_claim_leaves_exactly_one_file_on_disk(self):
        """Whatever the exception story, the tree must not fan out."""
        store = self.make_store("demo")
        handoff = store.create(to="reviewer", sender="builder", title="One file only")
        self.race_once(store, handoff.id)

        paths = store.paths
        surviving = sorted(
            path.name
            for state in STATES
            for path in paths.state_dir(state).rglob("*.md")
            if not path.name.startswith(".")
        )
        self.assertEqual(surviving, [f"{handoff.id}.md"])


class TransitionAgainstOpenReadersTests(IsolatedHomeTestCase):
    """A transition must survive a watcher that has the file open.

    Not a hypothetical: both watchers poll ``pending/`` every couple of seconds
    and open every file to parse it. Windows refuses to rename a file anyone has
    open (``PermissionError`` / WinError 32), so without a retry a claim issued
    at the wrong instant fails with an opaque sharing violation -- a handoff that
    "sometimes cannot be claimed".
    """

    def test_a_claim_succeeds_once_a_brief_reader_lets_go(self):
        store = self.make_store("demo")
        handoff = store.create(to="reviewer", sender="builder", title="brief reader")
        reader = open(handoff.path, "r", encoding="utf-8")
        self.addCleanup(reader.close)
        # Release on the same timescale a watcher parse takes.
        threading.Timer(0.15, reader.close).start()

        claimed = store.claim(handoff.id, by="reviewer")
        self.assertEqual(claimed.status, "claimed")

    def test_a_watcher_polling_concurrently_never_breaks_a_claim(self):
        store = self.make_store("demo")
        watcher = Watcher(
            [WatchTarget("demo", store.paths)],
            seat="reviewer",
            state_path=store.paths.state / "poll.json",
            interval=0.01,
            announce_backlog=True,
        )
        stop = threading.Event()

        def spin():
            while not stop.is_set():
                watcher.poll()

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)

        for index in range(12):
            handoff = store.create(to="reviewer", sender="builder", title=f"c{index}")
            with self.subTest(index=index):
                # The assertion is simply that this does not raise.
                self.assertEqual(store.claim(handoff.id, by="reviewer").status, "claimed")

    def test_a_reader_that_never_lets_go_gives_an_actionable_error(self):
        store = self.make_store("demo")
        handoff = store.create(to="reviewer", sender="builder", title="stuck reader")
        stuck = open(handoff.path, "r", encoding="utf-8")
        self.addCleanup(stuck.close)

        try:
            store.claim(handoff.id, by="reviewer")
        except CollabKitError as exc:
            # Windows: must be a typed error carrying a hint, never a bare OSError.
            self.assertTrue(exc.hint, "a sharing violation must explain what to do")
            self.assertIn("open", exc.hint)
        else:
            # POSIX renames happily over an open file; that is correct there.
            self.assertFalse(
                sys.platform.startswith("win"),
                "Windows should not have been able to rename a file held open",
            )
