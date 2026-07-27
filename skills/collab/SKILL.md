---
name: collab
description: Front door for collab-kit builder/reviewer collabs - use when asked to start a collab or new project, send or check handoffs, claim/answer/close a handoff, see what is outstanding across collabs, or get a diff reviewed by an independent reviewer or an adversarial regression hunt.
---

# /collab

File-based handoffs between two agent seats: **builder** (drives the work) and **reviewer**
(reads the actual diff and does not take the builder's claims at face value). State is markdown
files on disk under one collab root, moving `pending/ -> claimed/ -> done/ -> archive/`.

## 1. Find the active collab first

```bash
echo "$HANDOFF_ROOT"        # set  -> this session is pinned to one collab
handoff collabs             # unset -> list registered collabs, pick one
handoff status              # what is outstanding, everywhere
```

- `$HANDOFF_ROOT` set: use **`collab-handoff <cmd>`** (no collab name). This is the normal case
  inside a pinned session, and the form the templates document.
- `$HANDOFF_ROOT` unset: use **`handoff <collab> <cmd>`**, or set `HANDOFF_ROOT` for the session
  and switch to `collab-handoff`. Do not mix the two forms in one session.
- Read `$HANDOFF_ROOT/PROTOCOL.md` before acting. It holds this project's seats and its **sacred
  guardrails**, which are inviolable and are never inherited from another project.

## 2. What to do

| The user wants                | Do this |
|-------------------------------|---------|
| a new project / collab        | `newproject <name> --repo <git-url> --reviewer claude\|grok` (bootstrap script: scaffolds, clones, renders PROTOCOL/REVIEWER-BRIEFING/KICKOFF/context/IDEA and prints the session blocks). Queues only, no templates: `handoff new <name>` |
| a review of their work        | `collab-handoff create --to reviewer ...` |
| to know what is waiting       | `collab-handoff list --pending` / `collab-handoff counts` / `handoff status` |
| to work an item               | `collab-handoff claim <id>` then `collab-handoff show <id>` |
| to answer an item             | `collab-handoff reply <id> --title "..." --file <path>` |
| to close an item with no reply| `collab-handoff done <id> --note "..."` |
| an existing collab registered | `handoff register <name> --root <path>` |
| to know if the kit is healthy | `handoff doctor` |

Request a review:

```bash
collab-handoff create --to reviewer --priority high \
  --title "Review: <what changed>" --tag <risk-area> --file review-request.md
```

Say in the body what changed, base and head commits, where the diff is, and what you are unsure
about. Do not assert it is correct -- that is the reviewer's call.

Work the queue:

```bash
collab-handoff list --pending --to reviewer
collab-handoff claim <id> --as reviewer     # atomic rename; exactly one seat wins
collab-handoff show <id>
collab-handoff reply <id> --title "Findings: ..." --file findings.md   # closes the parent
```

Other verbs: `list --all|--claimed|--done|--archive [--to|--from|--priority|--tag|--ids|--json]`,
`archive [<id>|--all-done]`, `path [--repo|--pending|--logs]`, `log -n <count>`.
Priorities: `low|normal|high|urgent`. Bodies: `--file <path>` or `--body "..."` or `-` for stdin --
one of them, never two.

## 3. Watchers

Start the seat's watcher in the background once per session, so handoffs surface instead of
waiting to be noticed:

```bash
HANDOFF_ROOT=<collab root> python3 <kit dir>/tools/watch-for-claude-handoffs.py   # builder side
HANDOFF_ROOT=<collab root> python3 <kit dir>/tools/watch-for-grok-handoffs.py     # reviewer side
```

## 4. Adversarial regression hunt

**Mandatory** for any diff touching **money / safety / data-integrity / auth / concurrency**, or
anything named in that project's guardrails. Skip it for typos and comment changes -- it costs a
fleet of agents.

```bash
git -C <repo> diff <base>..<head> > <collab root>/context/review.diff
```

```js
Workflow({ scriptPath: "<kit dir>/tools/diff-regression-hunt.workflow.js",
           args: { repo: "<repo path>", diffPath: "<collab root>/context/review.diff",
                   guardrails: [ /* verbatim from PROTOCOL.md, one string per rule */ ],
                   areas: [ /* one { name, files, why } per coherent change; omit to auto-derive */ ] }})
```

A breaker agent attacks each fix-area, then independent verifiers try to refute every finding
(default verdict: refuted). Returns `{ confirmed, refuted }`.

**It runs in parallel with the reviewer handoff, not instead of it.** A clean hunt is not sign-off
and never justifies skipping the reviewer.

## 5. Non-negotiables

- The reviewer reads the **actual diff**; the builder's summary is a hypothesis, not evidence.
- A finding without `file:line` plus a concrete trigger is not a finding. Default verdict on any
  claim you could not verify yourself is **reject**.
- Guardrails in `PROTOCOL.md` are inviolable and only the human edits them. When a request is in
  tension with one, escalate rather than proceed.
- Claim before working an item. Unclaimed means nobody owns it.
