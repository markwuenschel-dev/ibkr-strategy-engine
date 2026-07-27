"""The toolchain is pinned in nine places; this stops them drifting apart.

Python 3.14 is stated as a hard requirement in the CI matrix, in three
executables' startup guards, in `install.sh`'s preflight, in `newproject`'s
preflight, in `handoff doctor`, and in the README. Nothing links those to each
other, so the realistic failure is not that one of them is wrong today -- it is
that someone bumps the matrix in six months, ships it, and a user on 3.13 gets
a `SyntaxError` from deep inside the kit instead of the clear "needs Python
3.14" message the design promises.

These tests are deliberately about *agreement*, not about the specific number:
change `REQUIRED_PYTHON` here and the failures tell you every file you still
have to touch.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.support import REPO_ROOT

REQUIRED_PYTHON = (3, 14)
REQUIRED_PYTHON_TEXT = "3.14"

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PACKAGE_JSON = REPO_ROOT / "package.json"
NODE_VERSION_FILE = REPO_ROOT / ".node-version"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class PythonVersionPinTests(unittest.TestCase):
    """Every gate that refuses an old interpreter must name the same version."""

    def test_the_ci_matrix_tests_exactly_the_supported_version(self):
        text = read(CI_WORKFLOW)
        match = re.search(r"^\s*python-version:\s*\[(?P<versions>[^\]]*)\]", text, re.M)
        self.assertIsNotNone(match, "no python-version matrix found in ci.yml")
        versions = [v.strip().strip("'\"") for v in match.group("versions").split(",") if v.strip()]
        self.assertEqual(
            versions,
            [REQUIRED_PYTHON_TEXT],
            "the CI matrix must test exactly the version the kit claims to support",
        )

    def test_the_lint_job_uses_the_same_python(self):
        text = read(CI_WORKFLOW)
        pinned = re.findall(r"^\s*python-version:\s*'([^']+)'", text, re.M)
        self.assertTrue(pinned, "lint job does not pin a python-version")
        for version in pinned:
            with self.subTest(version=version):
                self.assertEqual(version, REQUIRED_PYTHON_TEXT)

    def test_the_startup_guards_refuse_anything_older(self):
        major, minor = REQUIRED_PYTHON
        for name in ("tools/handoff", "tools/collab-handoff"):
            with self.subTest(entry_point=name):
                text = read(REPO_ROOT / name)
                self.assertIn(
                    f"sys.version_info < ({major}, {minor})",
                    text,
                    f"{name} does not refuse interpreters older than {REQUIRED_PYTHON_TEXT}",
                )
                self.assertIn(f"Python {REQUIRED_PYTHON_TEXT} or newer", text)

    def test_the_shell_preflights_require_the_same_version(self):
        major, minor = REQUIRED_PYTHON
        # install.sh and newproject write the tuple without a space; accept both
        # spellings so the test is about the version, not about formatting.
        pattern = re.compile(rf"sys\.version_info >= \({major},\s*{minor}\)")
        for name in ("install.sh", "bin/newproject"):
            with self.subTest(script=name):
                text = read(REPO_ROOT / name)
                self.assertRegex(text, pattern, f"{name} does not gate on {REQUIRED_PYTHON_TEXT}")
                self.assertIn(REQUIRED_PYTHON_TEXT, text)

    def test_doctor_reports_against_the_same_version(self):
        major, minor = REQUIRED_PYTHON
        text = read(REPO_ROOT / "tools" / "collabkit" / "cli.py")
        self.assertIn(f"sys.version_info >= ({major}, {minor})", text)

    def test_the_readme_states_the_same_requirement(self):
        text = read(REPO_ROOT / "README.md")
        self.assertIn(f"Python {REQUIRED_PYTHON_TEXT}", text)

    def test_no_file_still_advertises_a_superseded_version(self):
        # The previous floor was 3.9 and the old matrix carried 3.11/3.13.
        stale = re.compile(r"Python 3\.9|>= 3\.9|\(3, 9\)|\(3,9\)|'3\.11'|'3\.13'")
        offenders = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or _is_out_of_scope(path):
                continue
            if path.suffix in {".pyc"} or path.name == "test_toolchain_pins.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if stale.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], f"stale Python version references remain: {offenders}")


# Trees this test does not govern.
#
# Third-party code is obvious -- a virtualenv full of packaging/ and numpy/ will
# always mention old Pythons, and scanning 58MB of site-packages to discover that
# is pure noise.
#
# `engine/` is the interesting one: it is a *separate package* with its own
# `requires-python`, and its pyproject legitimately discusses ib_async's and
# ibapi's support matrices -- statements about other people's packages, not about
# what this project runs on. Scoping this test to collab-kit and asserting the
# engine's own pin separately (see EnginePackagePinTests) keeps both honest,
# rather than contorting a factual comment to satisfy a regex.
#
# `.claude/` is agent session state -- handoff notes, transcripts -- excluded
# from git via .git/info/exclude and never shipped. It records history, and
# history mentions old versions: a note reading "ibapi's classifiers stop at
# Python 3.9" is the same category of third-party fact as the engine pyproject
# above. Governing it would make an unrelated local artifact able to fail the
# suite, which is exactly the false positive this comment block exists to avoid.
_VENDORED = {".venv", "venv", "site-packages", "node_modules", ".git", "__pycache__", ".uv"}
_OTHER_PACKAGES = {"engine"}
_AGENT_STATE = {".claude"}


def _is_out_of_scope(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & (_VENDORED | _OTHER_PACKAGES | _AGENT_STATE))


class EnginePackagePinTests(unittest.TestCase):
    """The engine is a separate package; assert its pin rather than ignoring it."""

    PYPROJECT = REPO_ROOT / "engine" / "pyproject.toml"

    def test_the_engine_requires_the_same_python_as_the_kit(self):
        if not self.PYPROJECT.is_file():
            self.skipTest("engine package not present")
        text = read(self.PYPROJECT)
        self.assertIn(
            f'requires-python = ">={REQUIRED_PYTHON_TEXT}"',
            text,
            "the engine must require the same Python the rest of the repo pins",
        )

    def test_the_engine_keeps_its_dependencies_out_of_the_kit(self):
        # collab-kit's "no third-party packages" constraint is only meaningful
        # if the dependency-bearing package stays on its own side of the fence.
        if not self.PYPROJECT.is_file():
            self.skipTest("engine package not present")
        for name in ("pyproject.toml", "requirements.txt", "setup.py"):
            with self.subTest(file=name):
                self.assertFalse(
                    (REPO_ROOT / name).exists(),
                    f"{name} at the repo root would put dependencies above the stdlib-only kit",
                )


class NodeToolchainPinTests(unittest.TestCase):
    """pnpm is dev tooling, but CI depends on it, so its wiring must hold."""

    def setUp(self):
        self.package = json.loads(read(PACKAGE_JSON))

    def test_the_package_manager_is_pinned_to_an_exact_pnpm_version(self):
        # pnpm/action-setup reads this field, so a range here would let CI drift
        # onto a different pnpm than the one the lockfile was written with.
        declared = self.package.get("packageManager", "")
        self.assertRegex(
            declared,
            r"^pnpm@\d+\.\d+\.\d+$",
            "packageManager must pin an exact pnpm version",
        )

    def test_the_package_declares_no_dependencies(self):
        # The whole project constraint is "no third-party dependencies". pnpm is
        # here to pin a toolchain, not to start a dependency tree.
        for field in ("dependencies", "devDependencies", "peerDependencies"):
            with self.subTest(field=field):
                self.assertFalse(
                    self.package.get(field),
                    f"{field} must stay empty -- this package has no dependencies by design",
                )

    def test_the_package_is_private_and_never_published(self):
        self.assertIs(self.package.get("private"), True)

    def test_every_declared_script_target_exists(self):
        scripts = self.package.get("scripts", {})
        self.assertIn("check:workflow", scripts)
        referenced = re.findall(r"(scripts/[\w.-]+)", " ".join(scripts.values()))
        self.assertTrue(referenced, "no scripts/ file referenced from package.json")
        for relative in referenced:
            with self.subTest(script=relative):
                self.assertTrue(
                    (REPO_ROOT / relative).is_file(),
                    f"package.json references {relative}, which does not exist",
                )

    def test_ci_invokes_the_shared_check_script(self):
        # If CI ever goes back to an inline `node --check`, the no-op trap that
        # scripts/check-workflow.mjs guards against comes straight back.
        text = read(CI_WORKFLOW)
        self.assertIn("pnpm run check:workflow", text)

        # Comments *about* the trap are wanted; an actual invocation is not. So
        # strip comment lines before looking, or this test fails on its own
        # explanation.
        executable = [
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        ]
        offending = [line.strip() for line in executable if "node --check" in line]
        self.assertEqual(
            offending,
            [],
            "CI should call the shared script, not run node --check inline",
        )

    def test_the_node_version_file_pins_a_bare_major(self):
        self.assertTrue(NODE_VERSION_FILE.is_file(), ".node-version is missing")
        pinned = read(NODE_VERSION_FILE).strip()
        self.assertRegex(pinned, r"^\d+(\.\d+)*$")
        floor = self.package.get("engines", {}).get("node", "")
        self.assertTrue(floor, "engines.node must state the supported floor")
        # The pinned version must satisfy the declared floor.
        floor_major = int(re.sub(r"\D", "", floor.split(".")[0]) or 0)
        self.assertGreaterEqual(int(pinned.split(".")[0]), floor_major)

    def test_the_lockfile_is_committed(self):
        self.assertTrue(
            (REPO_ROOT / "pnpm-lock.yaml").is_file(),
            "pnpm install --frozen-lockfile in CI needs a committed lockfile",
        )
