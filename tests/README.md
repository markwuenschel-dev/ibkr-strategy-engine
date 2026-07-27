# collab-kit test suite

Standard library only: `unittest`, `unittest.mock`, `tempfile`, `threading`. No
pytest, no third-party mocking, **no network access in any test**. Matches the
kit's own hard constraint from `ARCHITECTURE.md`.

## Running

From the repository root:

```sh
python3 -m unittest discover -s tests -t . -v
```

One module:

```sh
python3 -m unittest tests.test_store -v
```

One class or one test:

```sh
python3 -m unittest tests.test_store.HandoffClaimRaceTests -v
python3 -m unittest tests.test_store.HandoffClaimRaceTests.test_exactly_one_thread_wins_a_contested_claim -v
```

`tests/support.py` derives the repo root from its own `__file__` and puts
`<repo>/tools` on `sys.path`, so nothing needs installing and nothing is
hardcoded. Tests are order-independent and safe to re-run.

Everything is in-process. `tests.support.run_cli(argv)` drives
`collabkit.cli.main` / `main_root` with stdout and stderr captured and returns
`(exit_code, stdout, stderr)`. The Telegram bridge is loaded by path
(`tools/telegram-bridge.py` is not an importable name) and always given a fake
client — `tests.support.FakeTelegramClient` — with the same method surface as
the real one.

`tests/test_workflow_script.py` shells out to `node --check` and skips itself
when `node` is not on `PATH`. That is the only external process the suite uses.

## The isolation rule

**No test may touch the real `$COLLAB_HOME` or the user's home directory.**

Every test inherits `tests.support.IsolatedHomeTestCase`, which:

- creates its own `tempfile.TemporaryDirectory`,
- points `COLLAB_HOME` (and `HANDOFF_ROOT`, where a test needs it) inside it via
  `unittest.mock.patch.dict(os.environ, ...)`,
- clears ambient scope a command must not read: `HANDOFF_ROOT`, `KIT_DIR`,
  `COLLAB_SEAT_ALIASES`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
- and **asserts** all of that in `setUp` *and* again on cleanup, comparing
  against the real `$COLLAB_HOME` snapshotted before any patching.

If you add a test, inherit that base class. If you need a second collab, use
`self.make_collab(name)` / `self.make_store(name)` rather than building paths by
hand — they land under the test's own home. To assert containment, use
`self.assert_inside(child, parent)`, which resolves both sides.

## Expose bugs, do not fix them

If a test finds a defect in the code under test, the test **stays red-shaped**:
write the assertion that states the correct behaviour, mark it
`@unittest.expectedFailure` (or `@expected_failure_on_windows` from
`tests.support` for a platform-specific defect), and put a one-line
`# BUG: ...` comment directly above it. Do not soften the assertion to match the
current behaviour, and do not change the code under test from here — a suite
quietly bent to fit broken code is worse than no suite.

`expected_failure_on_windows` exists because the suite has to stay green on
every platform: if a defect is only reachable on Windows, an unconditional
`expectedFailure` would report an *unexpected success* on POSIX, which unittest
counts as a failure. The marker applies only where the defect is reachable.

**Currently marked: none.** Two Windows-only defects were found while this suite
was being written and have since been fixed in `tools/collabkit/`; the tests that
found them stayed, as plain regression guards:

- `tests/test_store.py::HandoffClaimRaceTests::test_exactly_one_thread_wins_a_contested_claim`
  — `os.rename` is not exclusive across concurrent threads on Windows (measured:
  8 threads, 8 winners on the same handoff). `store._transition()` now decides
  the race with an `O_EXCL` `FileLock` and renames underneath it. Stubbing that
  lock out makes this test fail in most rounds, so the guard has teeth.
- `tests/test_locking.py::StaleLockTests::test_pid_alive_reports_a_dead_process_as_dead`
  — `os.kill(pid, 0)` on Windows raises `OSError`/WinError 87 rather than
  `ProcessLookupError`, so `_pid_alive()` called every dead process alive and
  stale-lock recovery never fired. `locking._pid_alive_windows()` now answers via
  `OpenProcess`/`GetExitCodeProcess`.

Both guards are cheap; do not delete them because they are currently green.

## Conventions

- One class per area; test names state the property being asserted, not the
  mechanics.
- `subTest` for table-driven cases, so one bad row does not hide the others.
- Assert on behaviour, exit codes from `collabkit.errors`, and parsed `--json`
  payloads — never on log or message text, which is allowed to change.
- No `time.sleep` longer than 0.2s anywhere; the whole suite finishes in well
  under a minute.
