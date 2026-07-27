"""Syntax-check the Workflow script -- and prove the check is not a no-op.

The trap this guards: ``node --check`` on a file containing a top-level
``export`` exits **0 even when the file has a real syntax error**, because node
takes the ESM marker as a signal to stop and re-parse elsewhere. A CI step that
runs ``node --check tools/*.workflow.js`` therefore passes on a file that cannot
run, which is worse than having no check at all.

The fix is to build a *checkable form*: strip the leading ``export `` so the
file is an ordinary script, and wrap the body in an async IIFE so its top-level
``await`` and top-level ``return`` are legal. Then -- and this is the part that
matters -- deliberately break a copy and assert the check actually catches it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest

from tests.support import IsolatedHomeTestCase, WORKFLOW_PATH

NODE = shutil.which("node")

# The Workflow runtime is deterministic: wall-clock and randomness are not
# available to a workflow body, and reaching for them fails at run time.
FORBIDDEN_APIS = ("Date.now(", "Math.random(", "new Date(")


def checkable_source(source: str) -> str:
    """Turn the workflow module into something ``node --check`` can judge."""
    body = re.sub(r"(?m)^export[ \t]+(?=const\b)", "", source)
    return "(async function(){\n" + body + "\n})();\n"


class WorkflowSourceTests(IsolatedHomeTestCase):
    """Checks that need nothing but the file itself."""

    def setUp(self):
        super().setUp()
        self.assertTrue(WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}")
        self.source = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_the_workflow_uses_no_nondeterministic_runtime_apis(self):
        for api in FORBIDDEN_APIS:
            with self.subTest(api=api):
                self.assertNotIn(api, self.source, f"the Workflow runtime forbids {api})")

    def test_the_checkable_form_has_no_module_syntax_left(self):
        rewritten = checkable_source(self.source)
        self.assertEqual(
            re.findall(r"(?m)^export\b", rewritten), [],
            "a leftover top-level export would make node --check silently pass",
        )
        self.assertIn("const meta = {", rewritten)


@unittest.skipUnless(NODE, "node is not on PATH")
class WorkflowSyntaxTests(IsolatedHomeTestCase):
    """The real syntax gate, plus proof that the gate has teeth."""

    def setUp(self):
        super().setUp()
        self.source = WORKFLOW_PATH.read_text(encoding="utf-8")

    def node_check(self, text: str, name: str) -> subprocess.CompletedProcess:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return subprocess.run(
            [NODE, "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_workflow_body_is_syntactically_valid(self):
        result = self.node_check(checkable_source(self.source), "workflow-check.js")
        self.assertEqual(
            result.returncode, 0,
            f"node --check rejected the workflow:\n{result.stderr}",
        )

    def test_the_syntax_check_actually_rejects_broken_code(self):
        # Without this, a green run above would prove nothing: the whole point
        # is that node --check can be silently inert.
        broken = checkable_source(self.source) + "\nconst OOPS = = 1\n"
        result = self.node_check(broken, "workflow-broken.js")
        self.assertNotEqual(
            result.returncode, 0,
            "node --check accepted a file with a top-level syntax error, "
            "so the check on the real file proves nothing",
        )
        self.assertIn("SyntaxError", result.stderr)
