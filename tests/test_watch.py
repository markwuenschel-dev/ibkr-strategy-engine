"""The polling watcher: what a seat is shown, once, and never twice.

Two design properties get most of the attention here because both are easy to
regress and expensive to lose:

* A cold start with no state file **primes** the backlog silently -- attaching a
  watcher to a collab with 40 open handoffs must not dump all 40 into the
  session as if they were new.
* An **unparseable** file is deliberately *not* marked seen. A handoff caught
  mid-write must be retried on the next poll, or the one message this system
  exists to deliver is the one it drops.
"""

from __future__ import annotations

from collabkit import frontmatter
from collabkit.watch import SeenSet, WatchTarget, Watcher, default_state_path

from tests.support import IsolatedHomeTestCase


class WatcherTestCase(IsolatedHomeTestCase):
    """Shared fixture: one collab, one home, watchers on demand."""

    def setUp(self):
        super().setUp()
        self.paths = self.make_collab("proj")
        self.store = self.make_store("proj")
        self.home_layout = self.home_paths()

    def watcher(self, *, seat: str = "reviewer", state: str = "seen.json", **kwargs):
        return Watcher(
            [WatchTarget.at(self.paths.root, "proj")],
            seat=seat,
            state_path=self.tmp / state,
            home=self.home_layout,
            **kwargs,
        )

    @staticmethod
    def titles(events):
        return [event.title for event in events]


class WatcherRoutingTests(WatcherTestCase):
    def test_a_seat_only_sees_handoffs_addressed_to_it(self):
        self.store.create(to="reviewer", sender="builder", title="For the reviewer")
        self.store.create(to="builder", sender="reviewer", title="For the builder")

        reviewer = self.watcher(seat="reviewer", state="reviewer.json", announce_backlog=True)
        builder = self.watcher(seat="builder", state="builder.json", announce_backlog=True)

        self.assertEqual(self.titles(reviewer.poll()), ["For the reviewer"])
        self.assertEqual(self.titles(builder.poll()), ["For the builder"])

    def test_a_broadcast_handoff_reaches_both_seats(self):
        self.store.create(to="all", sender="builder", title="Everyone")

        reviewer = self.watcher(seat="reviewer", state="reviewer.json", announce_backlog=True)
        builder = self.watcher(seat="builder", state="builder.json", announce_backlog=True)

        self.assertEqual(self.titles(reviewer.poll()), ["Everyone"])
        self.assertEqual(self.titles(builder.poll()), ["Everyone"])

    def test_events_are_ordered_most_urgent_first(self):
        self.store.create(to="reviewer", sender="builder", title="low", priority="low")
        self.store.create(to="reviewer", sender="builder", title="urgent", priority="urgent")
        self.store.create(to="reviewer", sender="builder", title="normal")

        events = self.watcher(announce_backlog=True).poll()

        self.assertEqual(self.titles(events)[0], "urgent")
        self.assertEqual(self.titles(events)[-1], "low")

    def test_only_pending_handoffs_are_surfaced(self):
        handoff = self.store.create(to="reviewer", sender="builder", title="Already taken")
        self.store.claim(handoff.id, by="reviewer")
        self.assertEqual(self.watcher(announce_backlog=True).poll(), [])

    def test_the_reviewer_seat_does_not_surface_inbound_phone_messages(self):
        # Only the seat talking to the human speaks for the human.
        inbox = self.home_layout.inbox_for("proj")
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "from-user-20260101T000000Z-000.md").write_text(
            frontmatter.dumps({"from": "user"}, "ping from the phone"), encoding="utf-8"
        )

        reviewer = self.watcher(seat="reviewer", state="reviewer.json", announce_backlog=True)
        builder = self.watcher(seat="builder", state="builder.json", announce_backlog=True)

        self.assertEqual(reviewer.poll(), [])
        builder_events = builder.poll()
        self.assertEqual([event.kind for event in builder_events], ["message"])
        self.assertEqual(builder_events[0].body, "ping from the phone")

    def test_a_human_message_outranks_any_handoff(self):
        self.store.create(to="builder", sender="reviewer", title="urgent work", priority="urgent")
        inbox = self.home_layout.inbox_for("proj")
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "from-user-20260101T000000Z-000.md").write_text(
            frontmatter.dumps({"from": "user"}, "look at me"), encoding="utf-8"
        )

        events = self.watcher(seat="builder", announce_backlog=True).poll()

        self.assertEqual([event.kind for event in events], ["message", "handoff"])


class WatcherSeenSetTests(WatcherTestCase):
    def test_a_handoff_is_announced_once_and_not_again(self):
        self.store.create(to="reviewer", sender="builder", title="Only once")
        watcher = self.watcher(announce_backlog=True)

        self.assertEqual(len(watcher.poll()), 1)
        self.assertEqual(watcher.poll(), [])
        self.assertEqual(watcher.poll(), [])

    def test_the_seen_set_persists_across_watcher_instances(self):
        self.store.create(to="reviewer", sender="builder", title="Remembered")

        first = self.watcher(announce_backlog=True)
        self.assertEqual(len(first.poll()), 1)

        second = self.watcher(announce_backlog=True)  # same state path
        self.assertEqual(second.poll(), [], "a restart must not re-announce old work")

    def test_new_work_after_a_restart_is_still_announced(self):
        self.store.create(to="reviewer", sender="builder", title="Old")
        self.assertEqual(len(self.watcher(announce_backlog=True).poll()), 1)

        self.store.create(to="reviewer", sender="builder", title="New")
        self.assertEqual(self.titles(self.watcher(announce_backlog=True).poll()), ["New"])

    def test_a_handoff_for_the_other_seat_is_marked_seen_so_it_is_not_reparsed(self):
        self.store.create(to="builder", sender="reviewer", title="Not mine")
        watcher = self.watcher(seat="reviewer", announce_backlog=True)
        watcher.poll()
        key = f"handoff:proj:{next(iter(self.paths.pending.glob('*.md'))).stem}"
        self.assertIn(key, watcher.seen)

    def test_the_seen_set_is_bounded(self):
        state = self.tmp / "bounded.json"
        seen = SeenSet(state, limit=3)
        for index in range(10):
            seen.add(f"key-{index}")
        seen.flush()
        reloaded = SeenSet(state, limit=3)
        # Oldest dropped first: the newest three survive.
        self.assertNotIn("key-0", reloaded)
        self.assertIn("key-9", reloaded)

    def test_a_corrupt_state_file_is_treated_as_empty_rather_than_fatal(self):
        state = self.tmp / "corrupt.json"
        state.write_text("{ not json", encoding="utf-8")
        seen = SeenSet(state)
        self.assertNotIn("anything", seen)


class WatcherColdStartTests(WatcherTestCase):
    def test_a_cold_start_primes_the_backlog_silently(self):
        for index in range(3):
            self.store.create(to="reviewer", sender="builder", title=f"Backlog {index}")

        seen_events = []
        watcher = self.watcher(state="cold.json", on_event=seen_events.append)
        emitted = watcher.run(once=True)

        self.assertEqual(emitted, 0)
        self.assertEqual(seen_events, [])
        self.assertTrue((self.tmp / "cold.json").is_file(), "priming must persist a cursor")

    def test_work_arriving_after_a_cold_start_is_announced(self):
        self.store.create(to="reviewer", sender="builder", title="Was already here")
        watcher = self.watcher(state="cold.json")
        watcher.run(once=True)

        self.store.create(to="reviewer", sender="builder", title="Arrived later")
        self.assertEqual(self.titles(watcher.poll()), ["Arrived later"])

    def test_announce_backlog_surfaces_the_existing_queue_on_a_cold_start(self):
        for index in range(2):
            self.store.create(to="reviewer", sender="builder", title=f"Backlog {index}")

        seen_events = []
        watcher = self.watcher(
            state="loud.json", announce_backlog=True, on_event=seen_events.append
        )
        emitted = watcher.run(once=True)

        self.assertEqual(emitted, 2)
        self.assertEqual(len(seen_events), 2)

    def test_max_polls_bounds_a_run(self):
        watcher = self.watcher(state="bounded.json", announce_backlog=True, interval=0.2)
        self.store.create(to="reviewer", sender="builder", title="Seen once")
        self.assertEqual(watcher.run(max_polls=2), 1)


class WatcherRobustnessTests(WatcherTestCase):
    def test_an_unparseable_file_is_not_marked_seen_and_is_retried(self):
        path = self.paths.pending / "20260101T000000Z-aaaaaa-partial.md"
        path.write_text("---\nto: reviewer\nthis header never closes\n", encoding="utf-8")

        watcher = self.watcher(announce_backlog=True)
        key = "handoff:proj:20260101T000000Z-aaaaaa-partial"

        self.assertEqual(watcher.poll(), [])
        self.assertNotIn(key, watcher.seen, "a mid-write file must be retried, not skipped")

        path.write_text(
            frontmatter.dumps(
                {
                    "id": "20260101T000000Z-aaaaaa-partial",
                    "to": "reviewer",
                    "from": "builder",
                    "title": "Finished writing",
                },
                "the body",
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.titles(watcher.poll()), ["Finished writing"])
        self.assertIn(key, watcher.seen)

    def test_atomic_write_temp_files_are_ignored(self):
        (self.paths.pending / ".20260101T000000Z-bbbbbb-x.md.tmp").write_text(
            "half", encoding="utf-8"
        )
        self.assertEqual(self.watcher(announce_backlog=True).poll(), [])

    def test_a_missing_pending_directory_is_not_an_error(self):
        import shutil

        shutil.rmtree(self.paths.pending)
        self.assertEqual(self.watcher(announce_backlog=True).poll(), [])

    def test_watching_several_collabs_tags_each_event_with_its_collab(self):
        other = self.make_store("second")
        self.store.create(to="reviewer", sender="builder", title="From proj")
        other.create(to="reviewer", sender="builder", title="From second")

        watcher = Watcher(
            [
                WatchTarget.at(self.paths.root, "proj"),
                WatchTarget.at(other.paths.root, "second"),
            ],
            seat="reviewer",
            state_path=self.tmp / "multi.json",
            home=self.home_layout,
            announce_backlog=True,
        )

        events = watcher.poll()
        self.assertEqual(
            sorted((event.collab, event.title) for event in events),
            [("proj", "From proj"), ("second", "From second")],
        )

    def test_stop_ends_a_run(self):
        watcher = self.watcher(state="stopped.json", announce_backlog=True, interval=0.2)
        watcher.stop()
        self.assertEqual(watcher.run(), 0)

    def test_event_json_is_serializable_and_carries_the_handoff(self):
        import json

        handoff = self.store.create(to="reviewer", sender="builder", title="Payload")
        event = self.watcher(announce_backlog=True).poll()[0]
        payload = json.loads(json.dumps(event.to_json(), default=str))
        self.assertEqual(payload["kind"], "handoff")
        self.assertEqual(payload["collab"], "proj")
        self.assertEqual(payload["handoff"]["id"], handoff.id)


class WatcherStatePathTests(WatcherTestCase):
    def test_seat_and_scope_get_separate_state_files(self):
        builder = default_state_path("builder", "proj", self.home_layout)
        reviewer = default_state_path("reviewer", "proj", self.home_layout)
        other_scope = default_state_path("reviewer", "second", self.home_layout)

        self.assertNotEqual(builder, reviewer)
        self.assertNotEqual(reviewer, other_scope)
        for path in (builder, reviewer, other_scope):
            self.assert_inside(path, self.home)
