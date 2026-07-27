"""The ``handoff`` / ``collab-handoff`` command line, driven in-process.

Assertions are on **exit codes** from ``collabkit.errors`` and on parsed
``--json`` payloads, never on message text: the codes are the documented
contract that scripts and CI depend on, the prose is not.
"""

from __future__ import annotations

import json
import os

from collabkit.errors import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_INVALID,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
)

from tests.support import IsolatedHomeTestCase, run_cli


class CliDispatchTests(IsolatedHomeTestCase):
    """Argument shape: what is a command, what is a collab name, what is junk."""

    def test_help_exits_ok_and_prints_usage_to_stdout(self):
        code, out, _err = run_cli(["help"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.strip())

    def test_help_aliases_exit_ok(self):
        for argv in (["-h"], ["--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(argv)[0], EXIT_OK)

    def test_no_arguments_is_a_usage_error(self):
        code, out, _err = run_cli([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertTrue(out.strip(), "usage should still be shown")

    def test_version_exits_ok(self):
        code, out, _err = run_cli(["version"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("collab-kit", out)

    def test_an_unknown_command_on_a_valid_collab_is_a_usage_error(self):
        run_cli(["new", "demo"])
        self.assertEqual(run_cli(["demo", "frobnicate"])[0], EXIT_USAGE)

    def test_a_leading_option_instead_of_a_collab_name_is_a_usage_error(self):
        self.assertEqual(run_cli(["--nonsense"])[0], EXIT_USAGE)

    def test_a_traversal_shaped_collab_name_is_rejected(self):
        for name in ("bad/name", "..", "a\\b", "UPPER", "-x"):
            with self.subTest(name=name):
                code, _out, _err = run_cli([name, "list"])
                self.assertIn(code, (EXIT_INVALID, EXIT_USAGE))

    def test_a_bare_collab_name_defaults_to_listing_it(self):
        run_cli(["new", "demo"])
        bare = run_cli(["demo"])
        explicit = run_cli(["demo", "list"])
        self.assertEqual(bare[0], EXIT_OK)
        self.assertEqual(bare[0], explicit[0])

    def test_an_unregistered_collab_is_not_found(self):
        self.assertEqual(run_cli(["ghost", "list"])[0], EXIT_NOT_FOUND)


class CliWorkflowTests(IsolatedHomeTestCase):
    """The documented happy path, end to end, through the CLI only."""

    def setUp(self):
        super().setUp()
        code, _out, _err = run_cli(["new", "demo"])
        self.assertEqual(code, EXIT_OK)

    def create(self, **overrides) -> str:
        argv = ["demo", "create", "--to", "reviewer", "--title", "Please review the drain"]
        for key, value in overrides.items():
            argv += [f"--{key}", value]
        code, out, _err = run_cli(argv)
        self.assertEqual(code, EXIT_OK)
        return out.strip().splitlines()[-1]

    def test_new_registers_the_collab_so_collabs_lists_it(self):
        code, out, _err = run_cli(["collabs", "--json"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual([entry["name"] for entry in payload], ["demo"])
        self.assertTrue(payload[0]["exists"])

    def test_new_is_idempotent(self):
        self.assertEqual(run_cli(["new", "demo"])[0], EXIT_OK)
        self.assertEqual(len(json.loads(run_cli(["collabs", "--json"])[1])), 1)

    def test_create_then_list_then_claim_then_reply(self):
        handoff_id = self.create(body="please look at the drain")

        code, out, _err = run_cli(["demo", "list", "--json"])
        self.assertEqual(code, EXIT_OK)
        listed = json.loads(out)
        self.assertEqual([item["id"] for item in listed], [handoff_id])
        self.assertEqual(listed[0]["to"], "reviewer")
        self.assertEqual(listed[0]["status"], "pending")

        self.assertEqual(run_cli(["demo", "claim", handoff_id])[0], EXIT_OK)

        code, out, _err = run_cli(["demo", "show", handoff_id, "--json"])
        self.assertEqual(code, EXIT_OK)
        shown = json.loads(out)
        self.assertEqual(shown["status"], "claimed")
        self.assertEqual(shown["claimed_by"], "reviewer")
        self.assertEqual(shown["body"].strip(), "please look at the drain")

        code, out, _err = run_cli(
            ["demo", "reply", handoff_id, "--title", "Looked", "--body", "lgtm"]
        )
        self.assertEqual(code, EXIT_OK)
        reply_id = out.strip().splitlines()[-1]
        self.assertNotEqual(reply_id, handoff_id)

        counts = json.loads(run_cli(["demo", "counts", "--json"])[1])
        self.assertEqual(counts["pending"], 1)  # the reply
        self.assertEqual(counts["done"], 1)     # the closed parent

    def test_ids_may_be_abbreviated_to_an_unambiguous_prefix(self):
        handoff_id = self.create()
        self.assertEqual(run_cli(["demo", "claim", handoff_id[:-3]])[0], EXIT_OK)

    def test_claiming_twice_reports_a_conflict(self):
        handoff_id = self.create()
        self.assertEqual(run_cli(["demo", "claim", handoff_id])[0], EXIT_OK)
        self.assertEqual(run_cli(["demo", "claim", handoff_id])[0], EXIT_CONFLICT)

    def test_showing_an_unknown_id_is_not_found(self):
        self.assertEqual(run_cli(["demo", "show", "20990101T000000Z-ffffff-x"])[0], EXIT_NOT_FOUND)

    def test_archive_requires_the_handoff_to_be_done(self):
        handoff_id = self.create()
        self.assertEqual(run_cli(["demo", "archive", handoff_id])[0], EXIT_CONFLICT)
        self.assertEqual(run_cli(["demo", "done", handoff_id])[0], EXIT_OK)
        self.assertEqual(run_cli(["demo", "archive", handoff_id])[0], EXIT_OK)

    def test_archive_without_an_id_or_all_done_is_a_usage_error(self):
        self.assertEqual(run_cli(["demo", "archive"])[0], EXIT_USAGE)

    def test_archive_all_done_sweeps_and_reports(self):
        first = self.create()
        second = self.create()
        run_cli(["demo", "done", first])
        run_cli(["demo", "done", second])

        code, out, _err = run_cli(["demo", "archive", "--all-done", "--json"])

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(sorted(json.loads(out)), sorted([first, second]))
        self.assertEqual(json.loads(run_cli(["demo", "counts", "--json"])[1])["archive"], 2)

    def test_path_prints_a_path_under_the_collab_root(self):
        code, out, _err = run_cli(["demo", "path", "--pending"])
        self.assertEqual(code, EXIT_OK)
        self.assert_inside(out.strip(), self.home / "demo")

    def test_log_records_the_lifecycle_events(self):
        handoff_id = self.create()
        run_cli(["demo", "claim", handoff_id])
        code, out, _err = run_cli(["demo", "log", "--json"])
        self.assertEqual(code, EXIT_OK)
        events = [record["event"] for record in json.loads(out)]
        self.assertIn("create", events)
        self.assertIn("claim", events)

    def test_an_invalid_priority_is_rejected(self):
        code, _out, _err = run_cli(
            ["demo", "create", "--to", "reviewer", "--title", "x", "--priority", "hgh"]
        )
        self.assertNotEqual(code, EXIT_OK)

    def test_body_may_come_from_a_file(self):
        body_file = self.tmp / "body.md"
        body_file.write_text("from a file\n", encoding="utf-8")
        code, out, _err = run_cli(
            ["demo", "create", "--to", "reviewer", "--title", "Filed", "--file", str(body_file)]
        )
        self.assertEqual(code, EXIT_OK)
        handoff_id = out.strip().splitlines()[-1]
        shown = json.loads(run_cli(["demo", "show", handoff_id, "--json"])[1])
        self.assertEqual(shown["body"].strip(), "from a file")

    def test_a_missing_body_file_is_not_found(self):
        code, _out, _err = run_cli(
            ["demo", "create", "--to", "reviewer", "--title", "x", "--file",
             str(self.tmp / "nope.md")]
        )
        self.assertEqual(code, EXIT_NOT_FOUND)

    def test_body_and_file_together_are_a_usage_error(self):
        body_file = self.tmp / "body.md"
        body_file.write_text("x", encoding="utf-8")
        code, _out, _err = run_cli(
            ["demo", "create", "--to", "reviewer", "--title", "x",
             "--body", "inline", "--file", str(body_file)]
        )
        self.assertEqual(code, EXIT_USAGE)


class CliGlobalCommandTests(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        run_cli(["new", "demo"])

    def test_status_json_reports_per_collab_counts(self):
        run_cli(["demo", "create", "--to", "reviewer", "--title", "Waiting"])

        code, out, _err = run_cli(["status", "--json"])

        self.assertEqual(code, EXIT_OK)
        report = json.loads(out)
        self.assertEqual(len(report), 1)
        entry = report[0]
        for key in ("collab", "root", "missing", "counts", "stale", "waiting_on", "oldest"):
            self.assertIn(key, entry)
        self.assertEqual(entry["collab"], "demo")
        self.assertEqual(entry["counts"]["pending"], 1)
        self.assertEqual(entry["waiting_on"], ["reviewer"])

    def test_status_plain_output_exits_ok(self):
        self.assertEqual(run_cli(["status"])[0], EXIT_OK)

    def test_status_for_an_unknown_name_is_not_found(self):
        self.assertEqual(run_cli(["status", "--name", "ghost"])[0], EXIT_NOT_FOUND)

    def test_register_requires_an_existing_collab_tree(self):
        self.assertEqual(
            run_cli(["register", "empty", "--root", str(self.tmp / "nothing-here")])[0],
            EXIT_NOT_FOUND,
        )

    def test_register_then_unregister_round_trips(self):
        root = self.home / "second"
        run_cli(["new", "second", "--root", str(root)])
        run_cli(["unregister", "second"])
        self.assertEqual(
            [e["name"] for e in json.loads(run_cli(["collabs", "--json"])[1])], ["demo"]
        )
        self.assertEqual(run_cli(["register", "second", "--root", str(root)])[0], EXIT_OK)
        self.assertEqual(
            sorted(e["name"] for e in json.loads(run_cli(["collabs", "--json"])[1])),
            ["demo", "second"],
        )

    def test_registering_a_taken_name_elsewhere_needs_force(self):
        other = self.home / "other"
        run_cli(["new", "other", "--root", str(other)])
        self.assertEqual(run_cli(["register", "demo", "--root", str(other)])[0], EXIT_CONFLICT)
        self.assertEqual(
            run_cli(["register", "demo", "--root", str(other), "--force"])[0], EXIT_OK
        )

    def test_prune_drops_registry_entries_whose_directory_vanished(self):
        import shutil

        ghost_root = self.home / "ghost"
        run_cli(["new", "ghost", "--root", str(ghost_root)])
        shutil.rmtree(ghost_root)

        code, out, _err = run_cli(["prune", "--json"])

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["removed"], ["ghost"])

    def test_doctor_passes_on_a_healthy_home(self):
        code, out, _err = run_cli(["doctor", "--json"])
        self.assertEqual(code, EXIT_OK)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertTrue(any(check["check"] == "collab home" for check in report["checks"]))

    def test_doctor_fails_when_a_registered_collab_is_gone(self):
        import shutil

        shutil.rmtree(self.home / "demo")
        code, out, _err = run_cli(["doctor", "--json"])
        self.assertEqual(code, EXIT_ERROR)
        self.assertFalse(json.loads(out)["ok"])

    def test_doctor_plain_output_exits_ok_on_a_healthy_home(self):
        self.assertEqual(run_cli(["doctor"])[0], EXIT_OK)


class CollabHandoffScopingTests(IsolatedHomeTestCase):
    """``collab-handoff`` is root-scoped and knows nothing about the registry."""

    def test_without_a_root_or_handoff_root_it_is_a_usage_error(self):
        self.assertIsNone(os.environ.get("HANDOFF_ROOT"))
        self.assertEqual(run_cli(["list"], root_mode=True)[0], EXIT_USAGE)

    def test_it_works_on_an_unregistered_collab_via_handoff_root(self):
        paths = self.make_collab("unregistered")
        self.set_handoff_root(paths.root)

        code, out, _err = run_cli(
            ["create", "--to", "reviewer", "--title", "Scoped"], root_mode=True
        )
        self.assertEqual(code, EXIT_OK)
        handoff_id = out.strip().splitlines()[-1]

        listed = json.loads(run_cli(["list", "--json"], root_mode=True)[1])
        self.assertEqual([item["id"] for item in listed], [handoff_id])

    def test_an_explicit_root_beats_the_environment(self):
        ambient = self.make_collab("ambient")
        explicit = self.make_collab("explicit")
        self.set_handoff_root(ambient.root)

        code, out, _err = run_cli(
            ["create", "--root", str(explicit.root), "--to", "reviewer", "--title", "Here"],
            root_mode=True,
        )
        self.assertEqual(code, EXIT_OK)
        handoff_id = out.strip().splitlines()[-1]

        self.assertTrue((explicit.pending / f"{handoff_id}.md").is_file())
        self.assertEqual(list(ambient.pending.glob("*.md")), [])

    def test_a_root_without_a_handoffs_directory_is_not_found(self):
        empty = self.tmp / "not-a-collab"
        empty.mkdir()
        self.assertEqual(
            run_cli(["list", "--root", str(empty)], root_mode=True)[0], EXIT_NOT_FOUND
        )

    def test_help_and_no_arguments_behave_like_the_registry_cli(self):
        self.assertEqual(run_cli(["help"], root_mode=True)[0], EXIT_OK)
        self.assertEqual(run_cli([], root_mode=True)[0], EXIT_USAGE)
