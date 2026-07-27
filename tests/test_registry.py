"""``collabs.json`` -- the kit's only piece of shared mutable state.

Two things are being pinned here. First, tolerance: a hand-edited registry with
one bad entry must not lock the user out of the other nine, while a registry
that is not JSON at all must be fatal (continuing from an empty dict would make
the next ``register`` overwrite everything). Second, safety: register is a
read-modify-write under an advisory lock, so concurrent writers must not lose
each other's entries.
"""

from __future__ import annotations

import json
import threading

from collabkit.errors import ConflictError, NotFoundError, ValidationError
from collabkit.paths import CollabPaths
from collabkit.registry import CollabEntry, Registry

from tests.support import IsolatedHomeTestCase


class RegistryCrudTests(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        self.paths = self.home_paths()
        self.registry = Registry(self.paths)

    def collab_at(self, name: str):
        root = self.home / name
        CollabPaths.at(root, name).ensure()
        return root

    def test_a_missing_registry_file_is_an_empty_registry_not_an_error(self):
        self.assertFalse(self.paths.registry.exists())
        self.assertEqual(self.registry.load(), {})
        self.assertEqual(self.registry.names(), [])

    def test_register_then_get_returns_the_entry(self):
        root = self.collab_at("demo")
        entry = self.registry.register("demo", root, repo="git@example:x.git", reviewer="grok")

        self.assertEqual(entry.name, "demo")
        self.assertEqual(entry.root, root.resolve())

        fetched = self.registry.get("demo")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.root, root.resolve())
        self.assertEqual(fetched.repo, "git@example:x.git")
        self.assertEqual(fetched.reviewer, "grok")
        self.assertTrue(fetched.created)

    def test_get_returns_none_for_an_unknown_name_but_require_raises(self):
        self.assertIsNone(self.registry.get("nope"))
        with self.assertRaises(NotFoundError):
            self.registry.require("nope")

    def test_require_returns_the_entry_for_a_known_name(self):
        root = self.collab_at("demo")
        self.registry.register("demo", root)
        self.assertEqual(self.registry.require("demo").root, root.resolve())

    def test_unregister_removes_the_entry_and_leaves_the_files_alone(self):
        root = self.collab_at("demo")
        self.registry.register("demo", root)

        removed = self.registry.unregister("demo")

        self.assertEqual(removed.name, "demo")
        self.assertEqual(self.registry.names(), [])
        self.assertTrue(CollabPaths.at(root, "demo").exists(), "files must survive unregister")

    def test_unregistering_an_unknown_name_raises(self):
        with self.assertRaises(NotFoundError):
            self.registry.unregister("never-registered")

    def test_prune_drops_only_entries_whose_collab_is_gone(self):
        alive = self.collab_at("alive")
        self.registry.register("alive", alive)
        self.registry.register("ghost", self.home / "ghost")  # never scaffolded

        removed = self.registry.prune()

        self.assertEqual(removed, ["ghost"])
        self.assertEqual(self.registry.names(), ["alive"])

    def test_prune_on_a_healthy_registry_removes_nothing(self):
        self.registry.register("alive", self.collab_at("alive"))
        self.assertEqual(self.registry.prune(), [])
        self.assertEqual(self.registry.names(), ["alive"])

    def test_iterating_yields_entries_in_name_order(self):
        for name in ("zulu", "alpha", "mike"):
            self.registry.register(name, self.collab_at(name))
        self.assertEqual([entry.name for entry in self.registry], ["alpha", "mike", "zulu"])

    def test_registering_an_invalid_name_is_refused(self):
        with self.assertRaises(ValidationError):
            self.registry.register("../escape", self.home / "x")


class RegistryRepointTests(IsolatedHomeTestCase):
    """Silently repointing a name is how a collab gets lost."""

    def setUp(self):
        super().setUp()
        self.paths = self.home_paths()
        self.registry = Registry(self.paths)
        self.first = self.home / "first"
        self.second = self.home / "second"
        for root in (self.first, self.second):
            CollabPaths.at(root, root.name).ensure()

    def test_re_registering_the_same_name_at_a_different_root_is_a_conflict(self):
        self.registry.register("demo", self.first)
        with self.assertRaises(ConflictError):
            self.registry.register("demo", self.second)
        self.assertEqual(self.registry.require("demo").root, self.first.resolve())

    def test_force_repoints_the_name(self):
        self.registry.register("demo", self.first)
        entry = self.registry.register("demo", self.second, force=True)
        self.assertEqual(entry.root, self.second.resolve())
        self.assertEqual(self.registry.require("demo").root, self.second.resolve())

    def test_re_registering_at_the_same_root_is_idempotent(self):
        first = self.registry.register("demo", self.first, repo="git@example:x.git")
        again = self.registry.register("demo", self.first)
        self.assertEqual(again.root, first.root)
        # Fields already recorded are carried forward, not blanked.
        self.assertEqual(again.repo, "git@example:x.git")
        self.assertEqual(again.created, first.created)


class RegistryParsingTests(IsolatedHomeTestCase):
    """Reading files a human hand-edited."""

    def setUp(self):
        super().setUp()
        self.paths = self.home_paths()
        self.registry = Registry(self.paths)

    def write_registry(self, payload) -> None:
        self.paths.registry.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )

    def test_the_bare_string_entry_form_is_accepted(self):
        root = self.home / "demo"
        CollabPaths.at(root, "demo").ensure()
        self.write_registry({"demo": str(root)})

        entries = self.registry.load()

        self.assertEqual(list(entries), ["demo"])
        self.assertEqual(entries["demo"].root, root.resolve())

    def test_a_relative_root_resolves_against_collab_home(self):
        CollabPaths.at(self.home / "demo", "demo").ensure()
        self.write_registry({"version": 1, "collabs": {"demo": {"root": "demo"}}})
        self.assertEqual(self.registry.load()["demo"].root, (self.home / "demo").resolve())

    def test_a_malformed_file_is_fatal(self):
        self.write_registry("{ this is not json")
        with self.assertRaises(ValidationError):
            self.registry.load()

    def test_a_single_malformed_entry_is_skipped_without_killing_the_rest(self):
        root = self.home / "good"
        CollabPaths.at(root, "good").ensure()
        self.write_registry(
            {
                "version": 1,
                "collabs": {
                    "good": {"root": str(root)},
                    "no-root": {},
                    "wrong-type": 42,
                    "blank-root": {"root": "   "},
                },
            }
        )
        self.assertEqual(sorted(self.registry.load()), ["good"])

    def test_unknown_entry_fields_are_preserved_through_a_rewrite(self):
        root = self.home / "demo"
        CollabPaths.at(root, "demo").ensure()
        self.write_registry(
            {"version": 1, "collabs": {"demo": {"root": str(root), "future_field": "keep me"}}}
        )
        self.assertEqual(self.registry.load()["demo"].extra, {"future_field": "keep me"})

        self.registry.register("other", self.home / "other")
        reloaded = json.loads(self.paths.registry.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["collabs"]["demo"]["future_field"], "keep me")

    def test_an_entry_without_a_root_is_rejected_by_the_parser(self):
        with self.assertRaises(ValidationError):
            CollabEntry.from_json("x", {}, home=self.home)
        with self.assertRaises(ValidationError):
            CollabEntry.from_json("x", 17, home=self.home)


class RegistryPersistenceTests(IsolatedHomeTestCase):
    """Saving is atomic, and keeps one generation of backup."""

    def setUp(self):
        super().setUp()
        self.paths = self.home_paths()
        self.registry = Registry(self.paths)

    def test_a_saved_registry_is_valid_json_with_a_schema_version(self):
        root = self.home / "demo"
        CollabPaths.at(root, "demo").ensure()
        self.registry.register("demo", root)

        payload = json.loads(self.paths.registry.read_text(encoding="utf-8"))

        self.assertEqual(payload["version"], 1)
        self.assertIn("demo", payload["collabs"])
        self.assertEqual(payload["collabs"]["demo"]["root"], str(root.resolve()))

    def test_the_second_save_leaves_a_parseable_backup_of_the_first(self):
        first = self.home / "first"
        second = self.home / "second"
        for root in (first, second):
            CollabPaths.at(root, root.name).ensure()

        self.registry.register("first", first)
        backup = self.paths.registry.with_suffix(".json.bak")
        self.assertFalse(backup.exists(), "nothing to back up before the first write")

        self.registry.register("second", second)

        self.assertTrue(backup.is_file())
        previous = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(sorted(previous["collabs"]), ["first"])
        current = json.loads(self.paths.registry.read_text(encoding="utf-8"))
        self.assertEqual(sorted(current["collabs"]), ["first", "second"])

    def test_no_temp_files_are_left_behind_by_a_save(self):
        self.registry.register("demo", self.home / "demo")
        leftovers = [p.name for p in self.home.glob("*.tmp")] + [
            p.name for p in self.home.glob(".*")
        ]
        self.assertEqual(leftovers, [])


class RegistryConcurrencyTests(IsolatedHomeTestCase):
    """Six writers, six entries -- the lock is what makes that true."""

    WRITERS = 6

    def test_concurrent_registrations_of_distinct_names_all_survive(self):
        paths = self.home_paths()
        names = [f"collab-{index}" for index in range(self.WRITERS)]
        for name in names:
            CollabPaths.at(self.home / name, name).ensure()

        errors: list[BaseException] = []
        guard = threading.Lock()
        gate = threading.Barrier(self.WRITERS)

        def register(name: str) -> None:
            gate.wait()
            try:
                Registry(paths).register(name, self.home / name)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                with guard:
                    errors.append(exc)

        threads = [threading.Thread(target=register, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [], f"registration failed under contention: {errors!r}")
        self.assertEqual(Registry(paths).names(), sorted(names))

    def test_the_registry_lock_file_is_released_after_a_write(self):
        paths = self.home_paths()
        Registry(paths).register("demo", self.home / "demo")
        self.assertFalse(paths.lock("registry").exists())
