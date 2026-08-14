"""The layering, enforced by import graph rather than by good intentions.

The settled shape is a one-way street. The operational tier -- the part that
knows about wall clocks, market sessions, child processes and long-lived
daemons -- is allowed to reach *down* into the options domain. The options
domain is never allowed to reach *up*::

    cli
     |- paperday ---------+
     |- scheduler --------+- runtime / process primitives
     |- market_calendar --+
     `- options.runner

    options/* must not import scheduler, paperday, or OS process code.

The failure this prevents is the one that never announces itself: a single
``from ..paperday import something`` inside ``options/`` and the domain can no
longer be exercised, tested, or reasoned about without dragging a session
controller and its process management along with it. By the time that hurts,
the edge has a dozen callers and removing it is a refactor rather than a
deletion. An import is cheap to add and expensive to take back, so the check
belongs at the moment of adding.

Checked structurally, over the AST, rather than by importing the modules:
a module that *cannot name* the operational tier cannot depend on it, however
the code inside changes later, and the check keeps working for modules that
would refuse to import in a test process.

Some of the operational modules are still being written. Rules that target a
module which does not exist yet skip with a reason rather than fail -- the
boundary is enforced the moment the file lands, and a red test for a file
nobody has written yet teaches nothing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

import engine

ENGINE_ROOT = Path(engine.__file__).resolve().parent
SRC_ROOT = ENGINE_ROOT.parent
OPTIONS_ROOT = ENGINE_ROOT / "options"

RUNTIME_MODULE = ENGINE_ROOT / "runtime.py"
MARKET_CALENDAR_MODULE = ENGINE_ROOT / "market_calendar.py"

# Rule 1. The operational tier, named as import targets. ``engine.scheduler``
# is listed whether or not the file exists yet: the point is that nothing in
# the domain may name it, and the day it lands is the worst day to start
# enforcing that.
OPERATIONAL_TIER = ("engine.paperday", "engine.scheduler", "engine.runtime")

# Rule 2. ``subprocess`` is forbidden outright -- there is no reading of the
# options domain in which spawning a child process is the right answer.
#
# ``os`` is *not* forbidden outright, and deliberately so. The domain already
# uses it for exactly the things a pure layer legitimately uses it for:
# ``os.environ`` (policy.py, approval.py), and the O_EXCL / fdopen / fsync /
# close dance that makes a journal append durable (approval.py, positions.py).
# A blanket ban on ``import os`` would fire on all of that, get suppressed with
# a noqa within a week, and enforce nothing.
#
# So the rule is narrowed to the thing actually being kept out: **starting,
# signalling, or replacing a process**. Attribute-level, on the names that do
# that and nothing else. Everything ``os`` offers for paths, environment and
# file descriptors stays available.
PROCESS_ATTRIBUTES = frozenset(
    {
        "abort",
        "_exit",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "plock",
        "popen",
        "startfile",
        "system",
        "wait",
        "wait3",
        "wait4",
        "waitid",
        "waitpid",
    }
)
# Families, matched by prefix: os.spawnl/spawnv/spawnvpe..., os.execl/execv...,
# os.posix_spawn/posix_spawnp. No other public ``os`` name begins with these,
# so the prefix match cannot catch a path or descriptor call by accident.
PROCESS_PREFIXES = ("spawn", "exec", "posix_spawn")

# Rule 3. ``runtime`` holds the process primitives everything else stands on.
# A leaf: it may be imported by the tier above it and must import none of it.
RUNTIME_FORBIDDEN = (
    "engine.paperday",
    "engine.scheduler",
    "engine.cli",
    "engine.options",
)

# Rule 4. ``market_calendar`` answers "is the market open, and when does it
# next open" from a clock and a table. Pure by construction: no processes, no
# filesystem, and no knowledge of this engine at all.
CALENDAR_FORBIDDEN_MODULES = ("subprocess", "pathlib", "os", "io", "engine")


@dataclass(frozen=True)
class Imported:
    """One dotted name a module imports, and where it said so."""

    module: Path
    name: str
    lineno: int
    source: str

    def __str__(self) -> str:
        location = self.module.relative_to(SRC_ROOT).as_posix()
        return f"{location}:{self.lineno}  {self.source}   [resolves to {self.name}]"


def _package_of(path: Path) -> str:
    """The dotted package a source file lives in, for resolving relative imports.

    ``src/engine/options/policy.py`` and ``src/engine/options/__init__.py`` both
    sit in package ``engine.options``, which is what ``level=1`` resolves against.
    """
    parts = path.resolve().relative_to(SRC_ROOT).with_suffix("").parts
    return ".".join(parts[:-1])


def _absolute(package: str, level: int, module: str | None) -> str:
    """Turn one ``ImportFrom`` target into an absolute dotted name.

    ``level`` is the number of leading dots. Inside ``engine.options``,
    ``from ..paperday import X`` (level 2, module "paperday") and
    ``from .. import paperday`` (level 2, module None) must both come out as
    something that matches ``engine.paperday`` -- the first through this
    function, the second through the alias expansion in :func:`_imports`.
    """
    if level == 0:
        return module or ""
    parts = package.split(".")
    if level > 1:
        parts = parts[: -(level - 1)]
    base = ".".join(parts)
    if not module:
        return base
    return f"{base}.{module}" if base else module


def _imports(path: Path) -> list[Imported]:
    """Every dotted name a file imports, in any form, resolved to absolute.

    ``from X import Y`` records both ``X`` and ``X.Y``, because ``Y`` may itself
    be a submodule -- ``from engine import paperday`` is the same dependency as
    ``import engine.paperday`` and has to be caught by the same rule.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    package = _package_of(path)
    found: list[Imported] = []

    def record(name: str, node: ast.stmt) -> None:
        if not name:
            return
        text = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
        found.append(Imported(path, name, node.lineno, text))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name, node)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(package, node.level, node.module)
            record(base, node)
            for alias in node.names:
                if alias.name != "*":
                    record(f"{base}.{alias.name}" if base else alias.name, node)
    return found


def _hits(name: str, forbidden: tuple[str, ...]) -> str | None:
    """The forbidden target ``name`` names, matching whole path components only.

    ``engine.options`` matches ``engine.options.runner`` but ``engine.runtime``
    does not match ``engine.runtime_helpers``, and nothing here matches
    ``typing.runtime_checkable``.
    """
    for target in forbidden:
        if name == target or name.startswith(f"{target}."):
            return target
    return None


def _os_aliases(tree: ast.AST) -> set[str]:
    """The local names bound to the ``os`` module in this file.

    Usually just ``{"os"}``; ``import os as _os`` binds ``_os``. Anything else
    called ``os`` in the file is a local variable and is not tracked, which is
    the conservative direction for a rule that must not cry wolf.
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os" or alias.name.startswith("os."):
                    aliases.add(alias.asname or "os")
    return aliases


def _process_control_uses(path: Path) -> list[str]:
    """Every place this file starts, signals, or replaces a process via ``os``.

    Two shapes: ``os.kill(...)`` as an attribute on the module, and
    ``from os import kill`` pulling the name in directly.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    aliases = _os_aliases(tree)
    location = path.relative_to(SRC_ROOT).as_posix()
    offences: list[str] = []

    def forbidden_attribute(attribute: str) -> bool:
        return attribute in PROCESS_ATTRIBUTES or attribute.startswith(PROCESS_PREFIXES)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and forbidden_attribute(node.attr)
        ):
            text = lines[node.lineno - 1].strip()
            offences.append(
                f"{location}:{node.lineno}  {text}   "
                f"[process control via {node.value.id}.{node.attr}]"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
            for alias in node.names:
                if forbidden_attribute(alias.name):
                    text = lines[node.lineno - 1].strip()
                    offences.append(
                        f"{location}:{node.lineno}  {text}   "
                        f"[process control via os.{alias.name}]"
                    )
    return offences


def options_modules() -> list[Path]:
    """Every source file under ``engine/options``, ``__init__`` included."""
    return sorted(OPTIONS_ROOT.rglob("*.py"))


def _report(rule: str, offences: list[str]) -> str:
    joined = "\n  ".join(offences)
    return f"{rule}\n  {joined}"


class TestTheOptionsDomainNeverReachesUp:
    """The domain must not depend on the tier that drives it.

    Both tests here walk every file under ``engine/options`` -- there is no
    allow-list and no exemption, because the first exemption is what the second
    one gets argued from.
    """

    def test_the_options_package_has_modules_to_check(self) -> None:
        """Guards the two rules below against passing over an empty set.

        A glob that silently matches nothing is the classic way a fitness test
        goes green forever without checking anything.
        """
        modules = options_modules()
        assert len(modules) > 10, (
            f"only {len(modules)} module(s) found under {OPTIONS_ROOT} -- the "
            "layering rules below would be vacuously true"
        )

    def test_no_options_module_imports_paperday_scheduler_or_runtime(self) -> None:
        """Rule 1. The direction of the arrow, in every import form.

        ``import engine.paperday``, ``from engine.paperday import X``,
        ``from ..paperday import X`` and ``from .. import paperday`` are the
        same dependency written four ways, and all four fail here.
        """
        offences = [
            f"{imported}   [forbidden: {target}]"
            for module in options_modules()
            for imported in _imports(module)
            if (target := _hits(imported.name, OPERATIONAL_TIER))
        ]
        assert not offences, _report(
            "the options domain imported the operational tier that drives it. "
            "The dependency runs one way: paperday/scheduler/runtime may import "
            "options, never the reverse. Move the shared piece down into "
            "options, or pass it in as an argument.",
            offences,
        )

    def test_no_options_module_uses_os_process_control(self) -> None:
        """Rule 2. No child processes, no signals, no exec -- and no ban on ``os``.

        ``subprocess`` is forbidden outright. ``os`` is forbidden only at the
        attribute level, for the names that start, signal, or replace a process
        (``kill``, ``popen``, ``system``, ``fork``, ``spawn*``, ``exec*``,
        ``wait*``, ``abort``, ``_exit``). ``os.environ``, ``os.path``,
        ``os.open``/``fdopen``/``fsync``/``close`` and the rest stay legal --
        the domain already relies on them for durable journal appends, and a
        rule that fired on those would be suppressed rather than obeyed.
        """
        offences: list[str] = []
        for module in options_modules():
            offences.extend(
                f"{imported}   [forbidden: subprocess]"
                for imported in _imports(module)
                if _hits(imported.name, ("subprocess",))
            )
            offences.extend(_process_control_uses(module))
        assert not offences, _report(
            "the options domain reached for OS process control. Spawning, "
            "signalling or replacing a process belongs in the runtime layer; "
            "the domain should be callable from a test with no children and no "
            "signal handlers.",
            offences,
        )


class TestTheRuntimeLayerIsALeaf:
    """``engine.runtime`` holds process primitives and must depend on nothing above.

    A runtime that imports ``paperday`` turns the bottom of the stack into a
    cycle, and every module in the diagram then transitively imports every
    other one.
    """

    def test_runtime_imports_nothing_from_the_layers_above_it(self) -> None:
        """Rule 3. Skipped, not failed, while the module is still unwritten."""
        if not RUNTIME_MODULE.exists():
            pytest.skip(
                f"{RUNTIME_MODULE.relative_to(SRC_ROOT).as_posix()} does not exist "
                "yet -- this boundary applies the moment the module lands"
            )
        offences = [
            f"{imported}   [forbidden: {target}]"
            for imported in _imports(RUNTIME_MODULE)
            if (target := _hits(imported.name, RUNTIME_FORBIDDEN))
        ]
        assert not offences, _report(
            "engine.runtime is a leaf: it may be imported by paperday, "
            "scheduler, cli and options, and must import none of them. What it "
            "needs from a caller is an argument, not an import.",
            offences,
        )


class TestTheMarketCalendarIsPure:
    """``engine.market_calendar`` answers session questions from a clock and a table.

    It is the module most likely to be consulted from anywhere, so it is the
    one that must cost nothing to consult: no engine imports to drag in, no
    file to read, no process to start. Keeping it pure is what makes session
    edges testable by passing a datetime.
    """

    def test_the_market_calendar_touches_no_process_no_disk_and_no_engine(
        self,
    ) -> None:
        """Rule 4. Skipped, not failed, while the module is still unwritten."""
        if not MARKET_CALENDAR_MODULE.exists():
            pytest.skip(
                f"{MARKET_CALENDAR_MODULE.relative_to(SRC_ROOT).as_posix()} does "
                "not exist yet -- this boundary applies the moment the module lands"
            )
        offences = [
            f"{imported}   [forbidden: {target}]"
            for imported in _imports(MARKET_CALENDAR_MODULE)
            if (target := _hits(imported.name, CALENDAR_FORBIDDEN_MODULES))
        ]

        source = MARKET_CALENDAR_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MARKET_CALENDAR_MODULE))
        lines = source.splitlines()
        location = MARKET_CALENDAR_MODULE.relative_to(SRC_ROOT).as_posix()
        offences.extend(
            f"{location}:{node.lineno}  {lines[node.lineno - 1].strip()}   "
            "[forbidden: builtin open()]"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        )
        assert not offences, _report(
            "engine.market_calendar must stay a pure function of the clock: no "
            "subprocess, no filesystem (pathlib/os/io/open), and no engine "
            "imports at all. A holiday table belongs in the source, not in a "
            "file it reads at import time.",
            offences,
        )
