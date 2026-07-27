"""``collabkit.dotenv``: parsing, precedence, discovery, and what it never leaks.

The precedence tests are the load-bearing ones. A dotenv loader that overrides a
real environment variable turns "I exported it and it still used the old value"
into an invisible bug, and this repo puts a broker account id through that path.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.support import IsolatedHomeTestCase

from collabkit import dotenv


class ParseTests(unittest.TestCase):
    def test_plain_pairs(self) -> None:
        values, problems = dotenv.parse("A=1\nB=two\n")
        self.assertEqual(values, {"A": "1", "B": "two"})
        self.assertEqual(problems, [])

    def test_blank_lines_and_comments_are_skipped(self) -> None:
        values, problems = dotenv.parse("\n# a comment\n\n  # indented\nA=1\n")
        self.assertEqual(values, {"A": "1"})
        self.assertEqual(problems, [])

    def test_export_prefix_is_tolerated(self) -> None:
        """People paste `export FOO=bar` straight out of shell instructions."""
        values, _ = dotenv.parse("export TELEGRAM_BOT_TOKEN=123:abc\n")
        self.assertEqual(values, {"TELEGRAM_BOT_TOKEN": "123:abc"})

    def test_value_may_contain_equals(self) -> None:
        values, _ = dotenv.parse("A=b=c=d\n")
        self.assertEqual(values["A"], "b=c=d")

    def test_hash_inside_a_value_is_literal(self) -> None:
        """A truncated token would present as an auth failure with no visible cause."""
        values, _ = dotenv.parse("TOKEN=abc#def\n")
        self.assertEqual(values["TOKEN"], "abc#def")

    def test_quotes_are_stripped(self) -> None:
        values, _ = dotenv.parse("A=\"quoted\"\nB='single'\n")
        self.assertEqual(values, {"A": "quoted", "B": "single"})

    def test_quoted_contents_stay_literal(self) -> None:
        """No escape decoding: a backslash in a token survives as a backslash."""
        values, _ = dotenv.parse('A="one\\ntwo"\nB=\'one\\ntwo\'\n')
        self.assertEqual(values["A"], "one\\ntwo")
        self.assertEqual(values["B"], "one\\ntwo")

    def test_quoting_preserves_surrounding_spaces(self) -> None:
        values, _ = dotenv.parse('A="  padded  "\nB=  bare  \n')
        self.assertEqual(values["A"], "  padded  ")
        self.assertEqual(values["B"], "bare")

    def test_malformed_lines_are_reported_with_line_numbers(self) -> None:
        values, problems = dotenv.parse("A=1\nnot a pair\n2BAD=x\n")
        self.assertEqual(values, {"A": "1"})
        self.assertEqual(len(problems), 2)
        self.assertIn("line 2", problems[0])
        self.assertIn("line 3", problems[1])

    def test_problems_never_contain_values(self) -> None:
        _, problems = dotenv.parse("2BAD=supersecret\nalsobad\n")
        for problem in problems:
            self.assertNotIn("supersecret", problem)


class LoadTests(IsolatedHomeTestCase):
    def write_env(self, text: str, *, directory: Path | None = None) -> Path:
        target = (directory or self.tmp) / ".env"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_loads_into_the_given_mapping(self) -> None:
        path = self.write_env("FOO=bar\n")
        environ: dict[str, str] = {}
        result = dotenv.load(path, environ=environ)
        self.assertEqual(environ["FOO"], "bar")
        self.assertEqual(result.loaded, ("FOO",))
        self.assertEqual(result.path, path)

    def test_the_real_environment_wins(self) -> None:
        path = self.write_env("FOO=from-file\n")
        environ = {"FOO": "from-shell"}
        result = dotenv.load(path, environ=environ)
        self.assertEqual(environ["FOO"], "from-shell")
        self.assertEqual(result.skipped, ("FOO",))
        self.assertEqual(result.loaded, ())

    def test_override_inverts_precedence_when_asked(self) -> None:
        path = self.write_env("FOO=from-file\n")
        environ = {"FOO": "from-shell"}
        dotenv.load(path, environ=environ, override=True)
        self.assertEqual(environ["FOO"], "from-file")

    def test_a_missing_file_is_not_an_error(self) -> None:
        result = dotenv.load(self.tmp / "nope" / ".env", environ={})
        self.assertFalse(result.found)
        self.assertEqual(result.loaded, ())

    def test_describe_never_contains_a_value(self) -> None:
        path = self.write_env("TOKEN=supersecret\nBAD LINE\n")
        result = dotenv.load(path, environ={})
        self.assertNotIn("supersecret", result.describe())
        self.assertIn(str(path), result.describe())


class DiscoveryTests(IsolatedHomeTestCase):
    def test_finds_the_file_in_a_parent_directory(self) -> None:
        """The engine runs from engine/; the shared .env sits at the repo root."""
        root = self.tmp / "repo"
        nested = root / "engine" / "src"
        nested.mkdir(parents=True)
        (root / ".env").write_text("FOO=bar\n", encoding="utf-8")

        found = dotenv.find_file(nested)
        self.assertEqual(found, root / ".env")

    def test_the_nearest_file_wins(self) -> None:
        root = self.tmp / "repo"
        nested = root / "engine"
        nested.mkdir(parents=True)
        (root / ".env").write_text("FOO=root\n", encoding="utf-8")
        (nested / ".env").write_text("FOO=nested\n", encoding="utf-8")

        self.assertEqual(dotenv.find_file(nested), nested / ".env")

    def test_env_file_override_wins_over_the_search(self) -> None:
        root = self.tmp / "repo"
        root.mkdir(parents=True)
        (root / ".env").write_text("FOO=root\n", encoding="utf-8")
        pinned = self.tmp / "pinned.env"
        pinned.write_text("FOO=pinned\n", encoding="utf-8")

        os.environ[dotenv.ENV_FILE] = str(pinned)
        self.addCleanup(os.environ.pop, dotenv.ENV_FILE, None)
        self.assertEqual(dotenv.find_file(root), pinned)

    def test_returns_none_when_nothing_is_found(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        os.environ[dotenv.ENV_FILE] = str(self.tmp / "does-not-exist.env")
        self.addCleanup(os.environ.pop, dotenv.ENV_FILE, None)
        self.assertIsNone(dotenv.find_file(empty))


class CachingTests(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        dotenv.reset_for_tests()
        self.addCleanup(dotenv.reset_for_tests)

    def test_an_implicit_load_is_cached_so_a_second_caller_sees_the_truth(self) -> None:
        """`engine doctor` reports what `engine` already loaded, not an empty pass."""
        root = self.tmp / "repo"
        root.mkdir()
        (root / ".env").write_text("CACHED_KEY=1\n", encoding="utf-8")
        os.environ[dotenv.ENV_FILE] = str(root / ".env")
        self.addCleanup(os.environ.pop, dotenv.ENV_FILE, None)
        self.addCleanup(os.environ.pop, "CACHED_KEY", None)

        first = dotenv.load()
        second = dotenv.load()
        self.assertEqual(first.loaded, ("CACHED_KEY",))
        self.assertEqual(second.loaded, ("CACHED_KEY",))
        self.assertIs(first, second)
        self.assertIs(dotenv.last_result(), first)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
