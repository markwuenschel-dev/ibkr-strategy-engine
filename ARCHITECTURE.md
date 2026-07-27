# Architecture

collab-kit is a file-based orchestration layer for running a **builder + independent-reviewer**
agent loop on a codebase, with a human in the loop over Telegram. The whole design treats agent
output like untrusted code and makes verification a first-class, parallel, independent step rather
than an afterthought.

## The core model

Two agent **seats**:

- **builder / coordinator** — drives the work and talks to you.
- **independent reviewer** — reads the actual diff and refuses to take the builder's claims at face
  value.

They're separate agent sessions — two Claude Code instances, or Claude + Grok, or any mix. The
premise: a lone agent self-certifies and ships subtle bugs; an independent reviewer *with repo
access* is what catches them. It's **agent-agnostic** — any CLI agent that can read/write files and
run shell commands works. The value isn't agent *count*, it's review depth + staying reachable.

## Handoff protocol

Communication is markdown files with YAML frontmatter (`to:`, `from:`, `priority:`, `id:`) moving
through per-project state directories:

```
<collab>/handoffs/
  pending/   → claimed/   → done/   → archive/
```

A small CLI manages it:

```
handoff <name> create --to <reviewer> --title "…" --priority normal --file body.md
handoff <name> list [--pending]
handoff <name> claim|show <id>
handoff status                     # cross-project overview: what's outstanding, where
handoff new <name>                 # scaffold + register an isolated collab
handoff register <name> --root <path>
```

A JSON registry (`collabs.json`) maps collab names → roots. Every project is **fully isolated** —
its own `handoffs/`, `logs/`, locks, and `context/` — so concurrent collabs can't race or
cross-contaminate. State lives entirely on disk, which makes it inspectable, crash-safe, and
trivially resumable: there's no daemon holding state in memory.

## Watchers

Each side runs a persistent monitor that tails its `pending/` directory and surfaces new handoffs
in-session, so the reviewer gets pinged the moment the builder requests a review (and vice versa):

```
HANDOFF_ROOT=$COLLAB_HOME/<name> monitor python3 tools/watch-for-claude-handoffs.py   # builder side
HANDOFF_ROOT=$COLLAB_HOME/<name> monitor python3 tools/watch-for-grok-handoffs.py     # reviewer side
```

An optional always-on alert watcher (`tools/watch-all-handoffs.py`) fans events across *every*
registered collab out to notifications, so nothing waits unseen.

## Phone bridge

`tools/telegram-bridge.py` is a zero-dependency (stdlib `urllib` long-poll) bridge over a
dead-simple file protocol:

```
agents write  $COLLAB_HOME/outbox/*.md              → forwarded to your Telegram chat
you send      /c <project> <message>  (Telegram)    → $COLLAB_HOME/inbox/live/<project>/from-user-*.md
                                                       (the live session's monitor picks it up)
```

Bring your own bot (a @BotFather token); the bridge learns-and-locks to a single chat id (set
`TELEGRAM_CHAT_ID` explicitly for a public bot), `/c` names are slug-sanitized against path
traversal, and outbox files are archived only on confirmed delivery. Because it's just an adapter
over the file protocol, you can swap in Slack/Discord/etc. the same way. Telegram is optional — the
core handoff loop works without it.

## Adversarial regression hunt

The interesting part. For any high-stakes diff — **money / safety / data-integrity / auth /
concurrency** — `tools/diff-regression-hunt.workflow.js` runs a workflow that *pipelines* over
fix-areas:

```
for each fix-area:
   probe   → a "breaker" agent tries to break THIS change (parallel across areas)
   verify  → each finding handed to an INDEPENDENT verifier that tries to REFUTE it
             (default: reject unless it can point to the exact code path + a concrete trigger)
→ return { confirmed, refuted }
```

So you get adversarial *generation* plus adversarial *verification* — the verification step kills
the plausible-but-wrong findings that make naive "ask an LLM to review this" useless. It runs **in
parallel with** the human-style reviewer, not instead of it: two independent lenses catch what one
misses. It's **risk-triggered** (you don't burn ~10–15 agents on a typo), parameterized per project
with that project's sacred guardrails, and baked into every generated `PROTOCOL.md`. In practice
it's caught real fix-introduced regressions a single review pass waved through.

One command:

```js
Workflow({ scriptPath: "tools/diff-regression-hunt.workflow.js",
           args: { repo, diffPath, guardrails, areas } })
```

## Bootstrapping

```
newproject <name> --repo <git-url> --reviewer claude|grok
```

Scaffolds + registers an isolated collab, clones the target repo, renders the per-project templates
(`PROTOCOL.md`, `REVIEWER-BRIEFING.md`, `KICKOFF.md`, `context/IDEA.md` — with the **guardrails**
written fresh per project, never inherited), and prints the session bootstrap block. Scripts
**self-locate** (`KIT_DIR` via `readlink`/`__file__`, `COLLAB_HOME` env-overridable), so it runs
under any user/path. One `install.sh` symlinks the CLI into `~/bin`, writes `COLLAB_HOME` to your
shell rc, and — for Claude Code users — installs a `/collab` skill that's the agent front-door.

## Layout

```
collab-kit/                # the kit (clone anywhere; this is the code)
  bin/        newproject · restart
  tools/      handoff · collab-handoff · watchers · telegram-bridge.py
              diff-regression-hunt.workflow.js · collab-kit/ (templates)
              collabkit/  # stdlib-only shared core, imported by all of the above:
                          # frontmatter · store (the state machine) · registry ·
                          # locking · atomic · paths · seats · watch · render · cli
  skills/collab/SKILL.md    # the Claude Code /collab front door
  tests/                    # stdlib unittest suite; python3 -m unittest discover -s tests -t .
  scripts/                  # check-workflow.mjs — the Workflow syntax gate, run by
                            # CI and humans alike via `pnpm run check:workflow`
  .github/workflows/ci.yml  # test matrix (3 OSes x Python 3.14) + shell/python/js lint
  install.sh · collabs.json.example · README.md
  package.json · pnpm-lock.yaml · .npmrc · .node-version   # dev toolchain only,
                            # zero dependencies; nothing at runtime shells out to Node

$COLLAB_HOME/               # your data (defaults to the kit dir; override via env)
  collabs.json              # registry of your collabs
  <name>/                   # one isolated collab: handoffs/ · context/ · PROTOCOL.md · …
  outbox/ · inbox/live/     # the Telegram bridge's queues
  logs/                     # watcher state, bridge state, locks (gitignored)
```

## Design rationale, in one line

Plain files + per-project isolation + an independent reviewer + adversarial verification — treat
agent output like untrusted code, and make verification a first-class, parallel, independent step.

## Requirements

Bash, Python 3.14, Git, and at least one agent CLI (Claude Code and/or Grok), authenticated the normal
way. Optionally a Telegram bot for the phone channel. No third-party Python packages. The kit sets
up the *collaboration system* — it does not provide the agent runtimes, model API keys, or any
model proxy.
