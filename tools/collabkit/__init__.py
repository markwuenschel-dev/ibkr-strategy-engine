"""collab-kit core library.

Stdlib-only support code shared by every executable in ``tools/`` and ``bin/``.
Hard constraint from ARCHITECTURE.md: **no third-party Python packages**, ever.
If you reach for a dependency here, write the small version instead.

The package name is ``collabkit`` (importable). Do not confuse it with the
sibling directory ``tools/collab-kit/``, which holds per-project markdown
*templates* and is deliberately not importable.

Module map
----------
errors       exception hierarchy + process exit codes
timeutil     UTC clock, ISO-8601 helpers, compact stamps
slug         slugification and path-traversal-safe name validation
atomic       crash-safe file writes (temp + os.replace)
locking      portable advisory locks (no fcntl; works on Windows)
frontmatter  strict, minimal YAML-subset frontmatter parse/serialize
paths        KIT_DIR / COLLAB_HOME discovery and per-collab directory layout
seats        canonical seat names and alias resolution
model        the Handoff record
store        the pending/claimed/done/archive state machine
registry     collabs.json -- the name -> root registry
render       {{PLACEHOLDER}} template rendering
notify       cross-platform desktop notification + outbox fan-out
log          append-only JSONL event log
watch        the polling engine behind every watcher script
"""

__all__ = ["__version__", "KIT_NAME"]

__version__ = "1.0.0"
KIT_NAME = "collab-kit"
