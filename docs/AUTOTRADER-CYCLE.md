# Unattended options cycle

This repository now has one persistent `options-cycle` worker for the paper-day
control plane. It owns one broker connection and one pacing ledger. The worker
does not launch a scanner and a runner for each tick.

## What runs and when

| Job | Cadence | Window | Work |
| --- | ---: | --- | --- |
| management | 5 minutes | regular session | reconcile positions, working orders, exits, and risk |
| breadth discovery | 30 minutes | pre-open/read-only | refresh a fair subset and evaluate every catalog symbol from the indexed cache |
| candidate probe | 10 minutes | open through 15 minutes before close | reuse the breadth cache and probe only the bounded shortlist |
| entry/reviewer service | 5 minutes | entry window 10:00–15:00 ET | service logical entries and reviewer responses; at most one new opening per eligible pass |

The timer is fixed-rate and durable. A missed slot is recorded and skipped; it
is never burst-replayed. Management and exits have priority over authorization,
candidate work, and discovery. A tick has a session id, lease nonce, tick id,
attempt id, policy digest, and catalog digest, and those identities follow its
receipts, scans, claims, reviews, and order saga.

## Universe behavior

The current checked-in catalog is the 80-symbol compatibility artifact:

`[autotrader-catalog-seed-80-v1.json](autotrader-catalog-seed-80-v1.json)`

Its current SHA-256 is:

`f2035e99260fddf6d2ddf27c7cb0f05150ea25e8b27dedeb48dcf5e196693276`

The artifact is loaded only with its operator-pinned path, version, and hash.
This compatibility seed is intentionally scan-only: its records have unknown
listing venue and unverified entitlement, so `automated_entry_allowed` is
false until an operator replaces it with a classified, entitlement-verified
artifact. It can prove breadth coverage without silently authorizing entry.
The breadth pass reads all configured symbols in one indexed cache operation;
it does not confuse a five-symbol deep shortlist with universe coverage. The
fair refresh ring records last observation, next due time, starvation age,
rank, request cost, and deferral reason. High-ranked symbols cannot permanently
displace never-seen or old symbols. Deep work reserves its estimated request
cost before asking the broker for expirations, strikes, qualification, quotes,
or liquidity data.

The immutable ScanBook is diagnostic-only unless its latest snapshot matches the
active catalog, policy, calendar, and scanner-config digests, is current, and
has complete breadth coverage. Claims live in a separate CAS ledger, so a later
scan cannot overwrite an earlier logical-entry claim.

## Policy and authority

The policy is strict `ibkr.autotrader/1` JSON. It must explicitly contain the
four cadences, calendar, windows, missed-tick policy, state directory, catalog
pin, discovery limits, entry limits, and pacing reserves. There are no code
defaults for those values. The scheduler verifies the policy bytes first, then
derives the worker command by adding the verified policy path, policy digest,
and state directory. A policy cannot smuggle those values through
`worker_command` or a separate CLI override.

`DRY_RUN` and `SHADOW` never create claims, review handoffs, approval
consumption, or opening transmissions. `REVIEW_ONLY` can create and service
review work but has no physical opening-send path. `ARMED` additionally
requires `--arm` inside the hash-pinned worker command and a live `FULL`
paper-day authority. Test success never enables `ARMED` by itself.

For an operator-reviewed deployment, calculate the artifact hashes first:

```powershell
$catalog = (Resolve-Path .\docs\autotrader-catalog-seed-80-v1.json).Path
$catalogSha = (Get-FileHash $catalog -Algorithm SHA256).Hash.ToLower()
$policy = (Resolve-Path C:\paperday\autotrader-policy.json).Path
$policySha = (Get-FileHash $policy -Algorithm SHA256).Hash.ToLower()
```

Then start the paper day with the same absolute state directory used by the
policy and scheduler. `FULL` additionally requires the reviewed configuration
hash; the controller publishes policy, catalog, configuration, session date,
fencing, scheduler, and reviewer-liveness facts into the authority record.

```powershell
.\bin\start-paper-day.ps1 `
  -StateDir 'C:\paperday\engine-state' `
  -Mandate FULL `
  -ScheduleConfig $policy `
  -ScheduleConfigSha256 $policySha `
  -PolicySha256 $policySha `
  -CatalogSha256 $catalogSha `
  -ConfigSha256 '<reviewed-64-hex-config-digest>' `
  -ConfigurationFingerprint '<base-64-hex-configuration-fingerprint>'
```

The policy's `catalog.path` must be the same absolute `$catalog` path and its
`catalog.sha256` must equal `$catalogSha`. The initial rollout should be
`SHADOW`, then `REVIEW_ONLY`, and only then an owner-approved paper `ARMED`
canary. Use the status command after start and after restart:

```powershell
.\bin\paper-day-status.ps1 -StateDir 'C:\paperday\engine-state'
```

`command_timeout_seconds` is the supervisor/one-shot command bound. The
persistent `options-cycle` process is deliberately not given a finite
`subprocess.run` timeout; its durable tick receipts, lease fencing, quiesce,
and recovery protocol own the lifetime of the worker. One-shot commands retain
the finite timeout.

## Restart and recovery

Before broker work the worker writes `TICK_STARTED`. It writes exactly one
terminal outcome: `TICK_FINISHED`, `TICK_ABORTED`, `TICK_UNRESOLVED`, or
`TICK_RECONCILED`. An unmatched tick blocks new entries; startup reconciles
positions, open orders, executions/journal state, execution outbox, and
unmatched scan receipts. A broker effect that cannot be proved is
`RECOVERY_REQUIRED`, not an instruction to replay the old tick.

Approval consumption and physical submission are separate durable outbox
steps. A crash after approval consumption, physical-send intent, or broker
acceptance therefore blocks new openings until reconciliation proves the
outcome. Approval and packet TTLs are checked independently at the final
transmission door, and every opening reprice rung consumes the same session
transmission budget.

Stop the session through the wrapper, not by deleting state files:

```powershell
.\bin\stop-paper-day.ps1 -StateDir 'C:\paperday\engine-state'
```

A missing or foreign scheduler identity, stale authority, corrupt receipt, or
unproven clean exit is dirty and remains visible for recovery. The absence of a
receipt is never treated as proof that nothing happened.

## NASDAQ expansion

The catalog provider already supports a hash-pinned operator artifact and
synthetic scale tests. A future importer can add NASDAQ symbols without
changing the worker. New symbols remain scan-visible but cannot become entry
candidates until classification, optionability, entitlement, and broker
identity are explicit. Expansion is gated by measured coverage age, venue
entitlements, pacing headroom, and starvation results at 250, 500, 1,000, and
larger catalog sizes.
