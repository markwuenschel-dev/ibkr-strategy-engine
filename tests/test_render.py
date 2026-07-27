"""``{{PLACEHOLDER}}`` rendering for the per-project scaffold.

Two properties carry weight beyond "it substitutes":

* Rendering is **strict**. A PROTOCOL.md that still says ``{{GUARDRAILS}}`` is
  worse than a failed scaffold, because the agent reads it as finished.
* Substitution is **single-pass**. A value that itself contains ``{{X}}`` is
  emitted literally, so user-supplied guardrail text cannot inject a
  placeholder that a second pass would then resolve.
"""

from __future__ import annotations

from collabkit import render
from collabkit.errors import ValidationError

from tests.support import IsolatedHomeTestCase


class RenderTextTests(IsolatedHomeTestCase):
    def test_placeholders_are_substituted(self):
        self.assertEqual(render.render("hi {{NAME}}", {"NAME": "bob"}), "hi bob")

    def test_whitespace_inside_the_braces_is_tolerated(self):
        for template in ("{{NAME}}", "{{ NAME }}", "{{  NAME  }}"):
            with self.subTest(template=template):
                self.assertEqual(render.render(template, {"NAME": "x"}), "x")

    def test_a_placeholder_may_appear_more_than_once(self):
        self.assertEqual(render.render("{{A}}-{{A}}", {"A": "z"}), "z-z")

    def test_only_uppercase_names_are_placeholders(self):
        # Lowercase braces are ordinary prose and must survive untouched.
        text = "{{lower}} and {{Mixed}} stay put"
        self.assertEqual(render.render(text, {}), text)

    def test_an_unresolved_placeholder_raises(self):
        with self.assertRaises(ValidationError) as caught:
            render.render("hi {{NAME}} and {{OTHER}}", {"NAME": "bob"})
        # The message names what is missing, so the caller can fix it in one go.
        self.assertIn("OTHER", caught.exception.message)

    def test_allow_missing_leaves_the_placeholder_in_place(self):
        self.assertEqual(
            render.render("hi {{NAME}} {{OTHER}}", {"NAME": "bob"}, allow_missing=True),
            "hi bob {{OTHER}}",
        )

    def test_substitution_is_single_pass_so_a_value_cannot_inject_a_placeholder(self):
        values = {"A": "{{B}}", "B": "SHOULD NOT APPEAR"}
        self.assertEqual(render.render("{{A}}", values), "{{B}}")

    def test_a_value_that_looks_like_a_template_is_emitted_literally(self):
        guardrails = "never call {{SECRET}} or {{ADMIN_TOKEN}}"
        rendered = render.render("Rules: {{GUARDRAILS}}", {"GUARDRAILS": guardrails})
        self.assertEqual(rendered, "Rules: " + guardrails)

    def test_non_string_values_are_coerced(self):
        self.assertEqual(render.render("{{N}}", {"N": 7}), "7")

    def test_placeholders_lists_every_name_in_a_template(self):
        self.assertEqual(
            render.placeholders("{{A}} {{B}} {{A}} {{lower}}"), {"A", "B"}
        )


class RenderFileTests(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.tmp / "PROTOCOL.md.tmpl"
        self.source.write_text("Project {{NAME}}\n", encoding="utf-8")
        self.destination = self.tmp / "PROTOCOL.md"

    def test_render_file_writes_the_substituted_text(self):
        written = render.render_file(self.source, self.destination, {"NAME": "demo"})
        self.assertEqual(written, self.destination)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "Project demo\n")

    def test_render_file_refuses_to_clobber_by_default(self):
        render.render_file(self.source, self.destination, {"NAME": "first"})
        with self.assertRaises(ValidationError):
            render.render_file(self.source, self.destination, {"NAME": "second"})
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "Project first\n")

    def test_overwrite_replaces_the_file(self):
        render.render_file(self.source, self.destination, {"NAME": "first"})
        render.render_file(self.source, self.destination, {"NAME": "second"}, overwrite=True)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "Project second\n")

    def test_render_file_is_strict_about_missing_values(self):
        with self.assertRaises(ValidationError):
            render.render_file(self.source, self.destination, {})
        self.assertFalse(self.destination.exists(), "a failed render must write nothing")


class RenderTreeTests(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        self.templates = self.tmp / "templates"
        (self.templates / "context").mkdir(parents=True)
        (self.templates / "PROTOCOL.md.tmpl").write_text("P {{NAME}}\n", encoding="utf-8")
        (self.templates / "context" / "IDEA.md.tmpl").write_text("I {{NAME}}\n", encoding="utf-8")
        (self.templates / "static.txt").write_text("no placeholders\n", encoding="utf-8")
        (self.templates / ".hidden.tmpl").write_text("{{NOPE}}\n", encoding="utf-8")
        self.out = self.tmp / "rendered"

    def test_the_tmpl_suffix_is_stripped_and_structure_is_preserved(self):
        written = render.render_tree(self.templates, self.out, {"NAME": "demo"})

        relative = sorted(str(path.relative_to(self.out).as_posix()) for path in written)
        self.assertEqual(relative, ["PROTOCOL.md", "context/IDEA.md", "static.txt"])
        self.assertEqual((self.out / "PROTOCOL.md").read_text(encoding="utf-8"), "P demo\n")
        self.assertEqual(
            (self.out / "context" / "IDEA.md").read_text(encoding="utf-8"), "I demo\n"
        )

    def test_files_without_the_suffix_are_copied_verbatim(self):
        render.render_tree(self.templates, self.out, {"NAME": "demo"})
        self.assertEqual(
            (self.out / "static.txt").read_text(encoding="utf-8"), "no placeholders\n"
        )

    def test_dotfiles_are_skipped(self):
        render.render_tree(self.templates, self.out, {"NAME": "demo"})
        self.assertFalse((self.out / ".hidden").exists())
        self.assertFalse((self.out / ".hidden.tmpl").exists())

    def test_existing_destinations_are_left_alone_unless_overwrite_is_set(self):
        render.render_tree(self.templates, self.out, {"NAME": "first"})
        second = render.render_tree(self.templates, self.out, {"NAME": "second"})

        self.assertEqual(second, [], "nothing should be rewritten on a re-scaffold")
        self.assertEqual((self.out / "PROTOCOL.md").read_text(encoding="utf-8"), "P first\n")

        render.render_tree(self.templates, self.out, {"NAME": "second"}, overwrite=True)
        self.assertEqual((self.out / "PROTOCOL.md").read_text(encoding="utf-8"), "P second\n")

    def test_a_missing_template_directory_raises(self):
        with self.assertRaises(ValidationError):
            render.render_tree(self.tmp / "nope", self.out, {})
