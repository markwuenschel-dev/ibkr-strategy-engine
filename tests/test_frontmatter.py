"""The frontmatter subset: what it promises to round-trip, and what it rejects.

The module's contract is a *round-trip guarantee* -- ``parse(dumps(meta))``
returns equal values for every type it emits -- because the state machine
rewrites a handoff's status in place and must not corrupt fields it does not
understand. These tests pin that guarantee type by type, then pin the failure
modes, because a parser that silently accepts garbage is how a half-written file
gets treated as a valid handoff.
"""

from __future__ import annotations

from collabkit import frontmatter
from collabkit.frontmatter import FrontmatterError

from tests.support import IsolatedHomeTestCase


class FrontmatterRoundTripTests(IsolatedHomeTestCase):
    """Values written by ``dumps`` come back from ``parse`` unchanged."""

    def assert_round_trips(self, key: str, value: object) -> None:
        text = frontmatter.dumps({key: value}, "body text")
        parsed, body = frontmatter.parse(text)
        self.assertIn(key, parsed)
        self.assertEqual(parsed[key], value)
        # bool is an int subclass; equality alone would let True == 1 pass.
        self.assertIs(type(parsed[key]), type(value))
        self.assertEqual(body.strip(), "body text")

    def test_every_supported_scalar_type_survives_a_round_trip(self):
        cases = {
            "a_string": "hello world",
            "an_int": 42,
            "a_negative_int": -7,
            "a_zero": 0,
            "a_float": 1.5,
            "a_negative_float": -0.25,
            "a_true": True,
            "a_false": False,
            "a_none": None,
        }
        for key, value in cases.items():
            with self.subTest(key=key, value=value):
                self.assert_round_trips(key, value)

    def test_lists_survive_a_round_trip_including_the_empty_list(self):
        cases = {
            "tags": ["security", "auth"],
            "single": ["only"],
            "mixed": ["text", 1, True, None],
            "empty": [],
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.assert_round_trips(key, value)

    def test_a_title_that_looks_like_a_number_round_trips_as_a_string(self):
        # The whole point of the quoting rule: `title: 2024` would come back an
        # int and the handoff would render its title as a number.
        for text in ("2024", "3.14", "-5", "0", "1e5", "0x10"):
            with self.subTest(title=text):
                parsed, _ = frontmatter.parse(frontmatter.dumps({"title": text}))
                self.assertIsInstance(parsed["title"], str)
                self.assertEqual(parsed["title"], text)

    def test_a_title_that_looks_like_a_bool_or_null_round_trips_as_a_string(self):
        for text in ("true", "false", "yes", "no", "on", "off", "null", "none", "~"):
            with self.subTest(title=text):
                parsed, _ = frontmatter.parse(frontmatter.dumps({"title": text}))
                self.assertIsInstance(parsed["title"], str)
                self.assertEqual(parsed["title"], text)

    def test_titles_needing_quotes_round_trip_verbatim(self):
        cases = {
            "colon": "Review: the auth fix",
            "colon_no_space": "Review:auth",
            "hash": "fix bug #42 in the drain",
            "leading_hash": "#42 is broken",
            "leading_space": "  indented title",
            "trailing_space": "trailing title  ",
            "both_spaces": "  padded  ",
            "brackets": "[draft] look at this",
            "braces": "{not a mapping}",
            "quotes": 'she said "no"',
            "single_quotes": "it's fine",
            "backslash": r"path\to\thing",
            "newline": "line one\nline two",
            "tab": "col\tcol",
            "leading_dash": "- not a list item",
            "empty": "",
            "comma": "one, two, three",
            "percent": "100% done",
        }
        for name, title in cases.items():
            with self.subTest(case=name):
                self.assert_round_trips("title", title)

    def test_key_order_puts_the_pinned_keys_first(self):
        text = frontmatter.dumps(
            {"z": 1, "id": "abc", "title": "t"}, key_order=["id", "title"]
        )
        lines = [line for line in text.splitlines() if ":" in line]
        self.assertEqual([line.split(":")[0] for line in lines], ["id", "title", "z"])

    def test_body_is_preserved_and_separated_from_the_header(self):
        text = frontmatter.dumps({"id": "x"}, "## Heading\n\n- a point\n")
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta, {"id": "x"})
        self.assertEqual(body.strip(), "## Heading\n\n- a point")


class FrontmatterParsingTests(IsolatedHomeTestCase):
    """Reading documents the kit did not write."""

    def test_block_and_inline_lists_produce_the_same_value(self):
        inline, _ = frontmatter.parse("---\ntags: [security, auth]\n---\n")
        block, _ = frontmatter.parse("---\ntags:\n  - security\n  - auth\n---\n")
        self.assertEqual(inline["tags"], ["security", "auth"])
        self.assertEqual(block["tags"], ["security", "auth"])

    def test_a_block_list_ends_when_the_next_key_starts(self):
        meta, _ = frontmatter.parse(
            "---\ntags:\n  - a\n  - b\ntitle: after the list\n---\n"
        )
        self.assertEqual(meta["tags"], ["a", "b"])
        self.assertEqual(meta["title"], "after the list")

    def test_explicit_empty_list_stays_a_list_but_a_bare_key_becomes_none(self):
        # These are genuinely different statements: "no tags" vs "unspecified".
        meta, _ = frontmatter.parse("---\ntags: []\nthread:\n---\n")
        self.assertEqual(meta["tags"], [])
        self.assertIsInstance(meta["tags"], list)
        self.assertIsNone(meta["thread"])

    def test_crlf_input_parses_identically_to_lf(self):
        crlf, crlf_body = frontmatter.parse("---\r\nid: x\r\ntags: [a]\r\n---\r\nbody\r\n")
        lf, lf_body = frontmatter.parse("---\nid: x\ntags: [a]\n---\nbody\n")
        self.assertEqual(crlf, lf)
        self.assertEqual(crlf_body, lf_body)
        self.assertNotIn("\r", crlf_body)

    def test_a_leading_utf8_bom_does_not_hide_the_frontmatter(self):
        # Escaped, never literal: an invisible character in a test fixture is a
        # trap for the next person to edit this file.
        bom = chr(0xFEFF)
        meta, body = frontmatter.parse(bom + "---\nid: x\n---\nbody\n")
        self.assertEqual(meta, {"id": "x"})
        self.assertEqual(body.strip(), "body")

    def test_whitespace_preceded_hash_opens_a_comment(self):
        meta, _ = frontmatter.parse("---\n# whole line comment\nid: value # trailing\n---\n")
        self.assertEqual(meta, {"id": "value"})

    def test_a_hash_inside_a_token_is_part_of_the_value(self):
        meta, _ = frontmatter.parse("---\nid: abc#1\nref: pr#1234\n---\n")
        self.assertEqual(meta["id"], "abc#1")
        self.assertEqual(meta["ref"], "pr#1234")

    def test_a_hash_inside_a_quoted_string_is_preserved(self):
        meta, _ = frontmatter.parse('---\ntitle: "has # inside"\nalt: \'also # here\'\n---\n')
        self.assertEqual(meta["title"], "has # inside")
        self.assertEqual(meta["alt"], "also # here")

    def test_double_quoted_escape_sequences_are_decoded(self):
        # BS is spelled with chr() so the fixtures stay readable as *source
        # text handed to the parser*, not as Python escapes the reader has to
        # unwind twice in their head.
        bs = chr(92)
        cases = [
            (bs + "n", "\n"),
            (bs + "t", "\t"),
            (bs + "r", "\r"),
            (bs + bs, bs),
            (bs + '"', '"'),
            (bs + "/", "/"),
            (bs + "u0041", chr(0x41)),
            (bs + "u00e9", chr(0xE9)),
            (bs + "u2713", chr(0x2713)),
        ]
        for escape, expected in cases:
            with self.subTest(escape=escape):
                meta, _ = frontmatter.parse('---\nv: "x%sy"\n---\n' % escape)
                self.assertEqual(meta["v"], "x" + expected + "y")

    def test_single_quotes_only_honour_the_doubled_quote_escape(self):
        meta, _ = frontmatter.parse("---\nv: 'it''s here \\n literal'\n---\n")
        self.assertEqual(meta["v"], "it's here \\n literal")

    def test_a_document_without_frontmatter_returns_empty_metadata_and_the_text(self):
        text = "# Just markdown\n\nNo header here.\n"
        self.assertEqual(frontmatter.parse(text), ({}, text))

    def test_an_empty_document_parses_to_empty_metadata_and_empty_body(self):
        self.assertEqual(frontmatter.parse(""), ({}, ""))

    def test_non_finite_floats_are_not_produced(self):
        # They are not JSON-serializable and would break --json downstream.
        for text in ("nan", "inf", "-inf", "Infinity"):
            with self.subTest(text=text):
                meta, _ = frontmatter.parse("---\nv: %s\n---\n" % text)
                self.assertIsInstance(meta["v"], str)

    def test_parse_file_reads_from_disk(self):
        path = self.tmp / "doc.md"
        path.write_text("---\nid: from-disk\n---\nbody\n", encoding="utf-8")
        meta, body = frontmatter.parse_file(path)
        self.assertEqual(meta["id"], "from-disk")
        self.assertEqual(body.strip(), "body")


class FrontmatterErrorTests(IsolatedHomeTestCase):
    """Malformed headers must fail loudly, with a line number."""

    def test_unterminated_frontmatter_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse("---\nid: x\ntitle: never closed\n")

    def test_a_duplicate_key_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse("---\nid: one\nid: two\n---\n")

    def test_an_inline_mapping_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse("---\nnested: {a: 1}\n---\n")

    def test_a_line_that_is_not_a_key_value_pair_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse("---\nnot a pair at all\n---\n")

    def test_an_unknown_escape_sequence_raises(self):
        for escape in (r"\q", r"\x41", r"\e"):
            with self.subTest(escape=escape):
                with self.assertRaises(FrontmatterError):
                    frontmatter.parse('---\nv: "a%sb"\n---\n' % escape)

    def test_a_truncated_unicode_escape_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse('---\nv: "a\\u00"\n---\n')

    def test_a_dangling_backslash_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse('---\nv: "trailing\\"\n---\n')

    def test_an_unterminated_quoted_string_raises(self):
        with self.assertRaises(FrontmatterError):
            frontmatter.parse('---\nv: "never closed\n---\n')

    def test_frontmatter_errors_are_validation_errors(self):
        from collabkit.errors import EXIT_INVALID, ValidationError

        with self.assertRaises(ValidationError) as caught:
            frontmatter.parse("---\nid: one\nid: two\n---\n")
        self.assertEqual(caught.exception.exit_code, EXIT_INVALID)
