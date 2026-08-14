# Paper-day authority and operator lifecycle

The paper-day wrappers use one absolute `StateDir`. The controller, scheduler,
manual engine commands, and status command must point at the same directory;
relative paths are rejected.

`MANAGE_ONLY` remains the compatibility mode. It permits reconciliation,
management, exits, and cancels without licensing unattended opening risk.
`FULL` is an explicit authority boundary and requires the scheduler policy
artifact plus all authority inputs:

- `PolicySha256` — the reviewed scheduler/auto-trader policy artifact;
- `ScheduleConfig` and `ScheduleConfigSha256` — the exact policy artifact
  passed to the persistent scheduler;
- `CatalogSha256` — the catalog snapshot used by the worker;
- `ConfigSha256` — the broker/risk/configuration artifact.
- `ConfigurationFingerprint` — the operator-supplied base configuration
  digest. FULL authority deterministically binds it to the policy, catalog,
  and config hashes before writing the effective gate fingerprint.

The hashes, session date, lock fencing token, scheduler session/nonce/PID,
reviewer liveness epoch, and absolute state root are published in `gate.json`.
An armed entry is refused when any of those facts is stale, missing, mismatched,
or when the scheduler heartbeat or reviewer liveness receipt is older than the
authority TTL.

## Lifecycle

```powershell
.\bin\start-paper-day.ps1 `
  -StateDir 'C:\paperday\engine-state' `
  -Mandate FULL `
  -ScheduleConfig 'C:\paperday\policy\autotrader-policy.json' `
  -ScheduleConfigSha256 '<64 hex characters>' `
  -PolicySha256 '<64 hex characters>' `
  -CatalogSha256 '<64 hex characters>' `
  -ConfigSha256 '<64 hex characters>' `
  -ConfigurationFingerprint '<64 hex characters>'

.\bin\paper-day-status.ps1 -StateDir 'C:\paperday\engine-state'
.\bin\restart-paper-day.ps1 -StateDir 'C:\paperday\engine-state' ...
.\bin\stop-paper-day.ps1 -StateDir 'C:\paperday\engine-state'
```

Restart is fail-closed: a dirty stop does not launch a replacement. A stop
with a configured scheduler requires either a matching durable clean-exit
receipt or a successful drain. A missing/dead PID without that proof is
`STOP_DIRTY`, and the gate records recovery as required.

`paper-day-status.ps1` reports the absolute state root, authority hashes,
entry-authority verdict, recovery state, scheduler PID/heartbeat authority, and
the latest durable tick receipt. A displayed PID or `OPEN` gate is never treated
as sufficient authority by itself.
