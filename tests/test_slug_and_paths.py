"""The path-traversal boundary: names in, safe path components out.

``slug`` is the kit's only security control against hostile names -- collab
names from the CLI and ``/c <project>`` names arriving from Telegram both land
here. The tests are written as *properties* rather than examples: whatever goes
in, the output alphabet is ``[a-z0-9-]`` and the constructed path is still
inside the home root.
"""

from __future__ import annotations

import re

from collabkit import slug
from collabkit.errors import ValidationError
from collabkit.paths import CollabPaths, HomePaths

from tests.support import IsolatedHomeTestCase

# The alphabet slugify promises. Anything outside it can express a traversal,
# a drive letter, an NTFS alternate data stream, or a leading shell flag.
SAFE_ALPHABET = re.compile(r"^[a-z0-9-]*$")

HOSTILE_NAMES = [
    "../../etc/passwd",
    "..",
    ".",
    "../",
    "..\\..\\windows\\system32",
    "C:\\Windows",
    "/etc/shadow",
    "\\\\server\\share",
    "name:stream",
    "-rf",
    "--force",
    "a b c",
    "UPPER",
    "MiXeD",
    "tab\there",
    "new\nline",
    "null\x00byte",
    "héllo wörld",
    "日本語",
    "🎉 party 🎉",
    "a" * 500,
    "",
    "   ",
    "con",
    "logs",
    "%2e%2e%2f",
    "$(whoami)",
    "`id`",
    "a;rm -rf /",
]


class ValidateNameTests(IsolatedHomeTestCase):
    """``validate_name`` rejects rather than repairs."""

    def test_traversal_and_separator_forms_are_rejected(self):
        for name in ("..", ".", "../..", "a/b", "a\\b", "/abs", "C:\\x", "a/../b"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    slug.validate_name(name)

    def test_empty_and_whitespace_only_names_are_rejected(self):
        for name in ("", "   ", "\t", "\n"):
            with self.subTest(name=repr(name)):
                with self.assertRaises(ValidationError):
                    slug.validate_name(name)

    def test_a_leading_dash_is_rejected_so_it_cannot_be_read_as_a_flag(self):
        for name in ("-x", "--force", "-"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    slug.validate_name(name)

    def test_uppercase_is_rejected_rather_than_folded(self):
        for name in ("Demo", "DEMO", "deMo"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    slug.validate_name(name)

    def test_windows_device_names_are_rejected_on_every_platform(self):
        for name in ("con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    slug.validate_name(name)

    def test_names_the_kit_reserves_for_its_own_directories_are_rejected(self):
        for name in ("logs", "outbox", "inbox", "archive", "tmp", "collabs"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    slug.validate_name(name)

    def test_an_over_length_name_is_rejected(self):
        too_long = "a" * (slug.MAX_NAME_LENGTH + 1)
        with self.assertRaises(ValidationError):
            slug.validate_name(too_long)
        # ...and the boundary itself is allowed.
        at_limit = "a" * slug.MAX_NAME_LENGTH
        self.assertEqual(slug.validate_name(at_limit), at_limit)

    def test_a_valid_name_is_returned_unchanged(self):
        for name in ("demo", "auth-refactor", "x", "a1", "2024-plan"):
            with self.subTest(name=name):
                self.assertEqual(slug.validate_name(name), name)
                self.assertTrue(slug.is_valid_name(name))

    def test_is_valid_name_is_the_non_raising_form(self):
        self.assertFalse(slug.is_valid_name("../x"))
        self.assertFalse(slug.is_valid_name("logs"))
        self.assertTrue(slug.is_valid_name("demo"))


class SlugifyTests(IsolatedHomeTestCase):
    """``slugify`` never raises and never leaves the safe alphabet."""

    def test_hostile_input_never_escapes_the_safe_alphabet(self):
        for raw in HOSTILE_NAMES:
            with self.subTest(raw=ascii(raw[:40])):
                produced = slug.slugify(raw)
                self.assertRegex(produced, SAFE_ALPHABET)
                self.assertNotIn("..", produced)
                self.assertNotIn("/", produced)
                self.assertNotIn("\\", produced)

    def test_output_is_bounded_by_max_length(self):
        for raw in ("a" * 500, "word " * 200, "x" * 65):
            with self.subTest(length=len(raw)):
                self.assertLessEqual(len(slug.slugify(raw)), slug.MAX_NAME_LENGTH)
        self.assertLessEqual(len(slug.slugify("a" * 500, max_length=10)), 10)

    def test_input_that_reduces_to_nothing_returns_the_fallback(self):
        for raw in ("", "   ", "...", "///", "🎉🎉", "\x00"):
            with self.subTest(raw=ascii(raw)):
                self.assertEqual(slug.slugify(raw, fallback="untitled"), "untitled")

    def test_a_reserved_result_returns_the_fallback(self):
        for raw in ("con", "CON", "logs", " NUL "):
            with self.subTest(raw=raw):
                self.assertEqual(slug.slugify(raw, fallback="untitled"), "untitled")

    def test_readable_text_keeps_its_words(self):
        self.assertEqual(slug.slugify("Review: the auth fix"), "review-the-auth-fix")
        self.assertEqual(slug.slugify("  Trailing  "), "trailing")
        self.assertEqual(slug.slugify("a--b---c"), "a-b-c")


class CoerceNameTests(IsolatedHomeTestCase):
    """The Telegram path: sanitize, then re-validate, or give up."""

    def test_every_hostile_name_yields_the_fallback_or_a_valid_name(self):
        for raw in HOSTILE_NAMES:
            with self.subTest(raw=ascii(raw[:40])):
                produced = slug.coerce_name(raw, fallback="FALLBACK")
                if produced == "FALLBACK":
                    continue
                self.assertRegex(produced, SAFE_ALPHABET)
                self.assertTrue(slug.is_valid_name(produced))

    def test_names_that_reduce_to_nothing_or_to_a_reserved_word_use_the_fallback(self):
        for raw in ("..", ".", "/", "\\", "🎉", "", "   ", "con", "logs", "NUL"):
            with self.subTest(raw=ascii(raw)):
                self.assertEqual(slug.coerce_name(raw, fallback="FALLBACK"), "FALLBACK")

    def test_the_default_fallback_is_the_empty_string(self):
        self.assertEqual(slug.coerce_name(".."), "")


class PathBoundaryTests(IsolatedHomeTestCase):
    """Whatever a name does, the resulting path stays under the home root."""

    def setUp(self):
        super().setUp()
        self.paths = HomePaths(self.home).ensure()

    def test_collab_root_either_raises_or_stays_inside_the_home_root(self):
        for raw in HOSTILE_NAMES:
            with self.subTest(raw=ascii(raw[:40])):
                try:
                    produced = self.paths.collab_root(raw)
                except ValidationError:
                    continue
                self.assert_inside(produced, self.home)

    def test_inbox_for_either_raises_or_stays_inside_inbox_live(self):
        for raw in HOSTILE_NAMES:
            with self.subTest(raw=ascii(raw[:40])):
                try:
                    produced = self.paths.inbox_for(raw)
                except ValidationError:
                    continue
                self.assert_inside(produced, self.paths.inbox_live)
                self.assert_inside(produced, self.home)

    def test_a_coerced_hostile_name_is_always_a_safe_inbox_path(self):
        # The full Telegram chain: chat text -> coerce_name -> inbox_for.
        for raw in HOSTILE_NAMES:
            coerced = slug.coerce_name(raw)
            if not coerced:
                continue
            with self.subTest(raw=ascii(raw[:40]), coerced=coerced):
                self.assert_inside(self.paths.inbox_for(coerced), self.paths.inbox_live)

    def test_a_valid_name_produces_exactly_one_path_component(self):
        produced = self.paths.collab_root("auth-refactor")
        self.assertEqual(produced.parent, self.home)
        self.assertEqual(produced.name, "auth-refactor")

    def test_collab_paths_never_reach_outside_their_own_root(self):
        collab = CollabPaths.at(self.home / "demo", "demo")
        for attribute in (
            "handoffs", "pending", "claimed", "done", "archive",
            "context", "logs", "locks", "state", "repo",
            "protocol", "briefing", "kickoff", "idea", "meta_file", "event_log",
        ):
            with self.subTest(attribute=attribute):
                self.assert_inside(getattr(collab, attribute), collab.root)
