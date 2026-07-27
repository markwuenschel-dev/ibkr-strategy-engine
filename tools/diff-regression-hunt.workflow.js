export const meta = {
  name: 'diff-regression-hunt',
  description: 'Adversarial regression hunt over a high-stakes diff: a breaker agent per fix-area, then independent verifiers that try to refute every finding.',
  whenToUse: 'Before shipping a diff that touches money, safety, data integrity, auth, or concurrency.',
  phases: [
    { title: 'Triage', detail: 'Classify diff risk; derive the fix-areas when the caller did not supply them.' },
    { title: 'Probe', detail: 'One adversarial breaker agent per fix-area tries to break that specific change.' },
    { title: 'Verify', detail: 'Every finding goes to independent verifiers whose job is to refute it.' },
  ],
}

// Schemas

const RISK_CATEGORIES = ['money', 'safety', 'data-integrity', 'auth', 'concurrency']

const SCHEMA_RISK = {
  type: 'object',
  additionalProperties: false,
  required: ['risk', 'categories', 'flaggedAreas', 'reason'],
  properties: {
    risk: { type: 'string', enum: ['low', 'medium', 'high'] },
    categories: { type: 'array', items: { type: 'string', enum: RISK_CATEGORIES } },
    flaggedAreas: { type: 'array', items: { type: 'string' }, description: 'Supplied fix-area names that carry real risk; empty if none do.' },
    reason: { type: 'string', description: 'One or two sentences citing the files that drove the verdict.' },
  },
}

const SCHEMA_AREA_ITEM = {
  type: 'object',
  additionalProperties: false,
  required: ['name', 'files', 'why'],
  properties: {
    name: { type: 'string', description: 'Short kebab-case handle for one coherent unit of change.' },
    files: { type: 'array', items: { type: 'string' }, description: 'Repo-relative paths this area spans.' },
    why: { type: 'string', description: 'What this part of the diff is trying to accomplish.' },
  },
}

const SCHEMA_AREAS = {
  type: 'object',
  additionalProperties: false,
  required: ['areas'],
  properties: { areas: { type: 'array', items: SCHEMA_AREA_ITEM } },
}

const SCHEMA_FINDING = {
  type: 'object',
  additionalProperties: false,
  required: ['title', 'file', 'line', 'severity', 'category', 'guardrailViolated', 'codePath', 'trigger', 'expected', 'actual', 'confidence'],
  properties: {
    title: { type: 'string', description: 'One line naming the regression, not the code smell.' },
    file: { type: 'string', description: 'Repo-relative path of the offending line.' },
    line: { type: 'integer', minimum: 0, description: 'Line number in that file; 0 only if genuinely unlocatable.' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    category: { type: 'string', enum: RISK_CATEGORIES.concat(['correctness', 'other']) },
    guardrailViolated: { type: 'string', description: 'The exact guardrail text violated, or "" if none.' },
    codePath: { type: 'string', description: 'Ordered file:line hops from entry point to the bug.' },
    trigger: { type: 'string', description: 'Concrete inputs and starting state that reach the bug.' },
    expected: { type: 'string' },
    actual: { type: 'string' },
    confidence: { type: 'number', minimum: 0, maximum: 1 },
  },
}

const SCHEMA_FINDINGS = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: { findings: { type: 'array', items: SCHEMA_FINDING } },
}

const SCHEMA_VERDICT = {
  type: 'object',
  additionalProperties: false,
  required: ['refuted', 'reason', 'evidence', 'counterExample'],
  properties: {
    refuted: { type: 'boolean', description: 'true = the finding does not hold. Default to true unless you proved otherwise yourself.' },
    reason: { type: 'string' },
    evidence: { type: 'string', description: 'file:line references you read yourself, not ones copied from the finding.' },
    counterExample: { type: 'string', description: 'If refuted: the code or input that defeats the claim. Otherwise "".' },
  },
}

// Constants

const SEVERITY_WEIGHT = { critical: 4, high: 3, medium: 2, low: 1 }
const DEFAULT_MAX_AREAS = 12
const DEFAULT_MAX_FINDINGS_PER_AREA = 8
const DEFAULT_VERIFIERS = 2

// Verifiers are assigned a lens by index so that two verifiers on the same finding
// attack it from genuinely different angles instead of agreeing with each other.
const VERIFIER_LENSES = [
  { id: 'does-the-code-path-actually-exist', instruction: 'Walk the claimed code path hop by hop in the real repo. If any hop is unreachable, already guarded, dead, or simply not what the file says, the finding is refuted.' },
  { id: 'does-the-trigger-actually-reproduce', instruction: 'Take the claimed trigger literally and trace it through the code. If those inputs and that starting state do not produce the claimed wrong behaviour, the finding is refuted.' },
  { id: 'is-this-pre-existing-rather-than-introduced-by-the-diff', instruction: 'Compare against the pre-diff state. If the same behaviour existed before this diff, the finding is refuted for this hunt - say so plainly; that is a valid refutation even when the behaviour is genuinely bad.' },
  { id: 'is-there-an-existing-guard-that-already-prevents-this', instruction: 'Search upstream for validation, locks, transactions, type constraints, or callers that make the bad state unreachable. One real guard refutes the finding.' },
  { id: 'is-the-severity-and-guardrail-claim-honest', instruction: 'Check whether the claimed guardrail violation and severity survive contact with the code. An inflated or fabricated guardrail citation refutes the finding as stated.' },
]

// Helpers

function toStringList(value) {
  const raw = typeof value === 'string' ? [value] : value
  if (!Array.isArray(raw)) return []
  return raw.filter((entry) => typeof entry === 'string' && entry.trim() !== '').map((entry) => entry.trim())
}

// Accepts ['name', ...] or [{ name, files, why }, ...] and returns the object form.
function normalizeAreas(raw) {
  if (!Array.isArray(raw)) return []
  const out = []
  for (const entry of raw) {
    if (typeof entry === 'string') {
      if (entry.trim()) out.push({ name: entry.trim(), files: [], why: '' })
      continue
    }
    if (!entry || typeof entry !== 'object' || typeof entry.name !== 'string' || !entry.name.trim()) continue
    out.push({
      name: entry.name.trim(),
      files: toStringList(entry.files),
      why: typeof entry.why === 'string' ? entry.why : '',
    })
  }
  return out
}

function clampInt(value, low, high, fallback) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  return Math.min(high, Math.max(low, Math.trunc(value)))
}

// Deterministic: severity desc, confidence desc, then stable string/number keys, so
// two runs over the same set of findings always report them in the same order.
function compareFindings(a, b) {
  const bySeverity = (SEVERITY_WEIGHT[b.severity] || 0) - (SEVERITY_WEIGHT[a.severity] || 0)
  if (bySeverity !== 0) return bySeverity
  const byConfidence = (b.confidence || 0) - (a.confidence || 0)
  if (byConfidence !== 0) return byConfidence
  if (a.file !== b.file) return a.file < b.file ? -1 : 1
  if (a.line !== b.line) return a.line - b.line
  if (a.title === b.title) return 0
  return a.title < b.title ? -1 : 1
}

// Fail closed. A finding is confirmed only when a strict majority of the verifiers that
// actually returned a verdict declined to refute it. Ties, and the case where every
// verifier died or was skipped, fall to refuted: an unreviewed finding is
// indistinguishable from a plausible-sounding hallucination, and shipping those as
// "confirmed" is the exact failure mode this workflow exists to prevent.
function isConfirmed(verdicts) {
  if (verdicts.length === 0) return false
  let standing = 0
  for (const verdict of verdicts) {
    if (verdict.refuted === false) standing += 1
  }
  return standing * 2 > verdicts.length
}

function describeArea(area) {
  const files = area.files.length ? area.files.join(', ') : '(not specified - locate them in the diff)'
  return `Fix-area: ${area.name}\nFiles: ${files}\nIntent: ${area.why || '(not specified - infer it from the diff)'}`
}

function describeGuardrails(guardrails) {
  if (guardrails.length === 0) return '  (none supplied for this run)'
  return guardrails.map((rule, i) => `  G${i + 1}. ${rule}`).join('\n')
}

function describeFinding(f) {
  return `Title: ${f.title}
Location: ${f.file}:${f.line}
Severity: ${f.severity} | Category: ${f.category} | Reporter confidence: ${f.confidence}
Guardrail claimed violated: ${f.guardrailViolated || '(none)'}
Claimed code path: ${f.codePath}
Claimed trigger: ${f.trigger}
Claimed expected: ${f.expected}
Claimed actual: ${f.actual}`
}

// Prompts

function riskPrompt(repo, diffPath, areas) {
  return `You are a cheap, fast risk classifier. Do not review the code - only decide whether it deserves an expensive adversarial review.

Repo: ${repo}
Diff: ${diffPath}
Candidate fix-areas: ${areas.length ? areas.map((a) => a.name).join(', ') : '(none supplied)'}

Read the diff at ${diffPath}. Open files under ${repo} only when a hunk is ambiguous on its own.

Decide whether this diff touches any of: money (pricing, orders, balances, fees, quantities), safety (kill switches, limits, guards), data integrity (persistence, migrations, idempotency, ordering), auth (identity, permissions, secrets), concurrency (locks, async, shared mutable state, retries).

Return 'low' only for changes that genuinely cannot cause harm: comments, docs, formatting, log strings, tests for unchanged behaviour, renames with no semantic effect. When in doubt, do not return low.

Report the risk level, the categories touched, the names of any supplied fix-areas that carry real risk, and a one-or-two-sentence reason citing specific files.`
}

function triagePrompt(repo, diffPath, maxAreas) {
  return `Split a diff into independent fix-areas. Do not review it.

Repo: ${repo}
Diff: ${diffPath}

Read the diff at ${diffPath}, and read the surrounding source under ${repo} wherever you need context.

Group the changed hunks into coherent fix-areas - one area per distinct behavioural change, not one per file. A single behavioural change spanning five files is ONE area; five unrelated changes inside one file are FIVE areas. Return at most ${maxAreas}, ordered most risk-bearing first.

For each area give a short kebab-case name, the repo-relative files it spans, and one sentence on what the change is trying to accomplish.`
}

function probePrompt(repo, diffPath, guardrails, area, maxFindings) {
  return `You are a breaker. Your job is to BREAK this specific change - to find a regression that this diff introduces. A reviewer has already read it for style; you are the second, hostile lens.

Repo: ${repo}
Diff: ${diffPath}

${describeArea(area)}

Project guardrails - sacred. Violating any one of these is AUTOMATICALLY a finding, at severity high or above:
${describeGuardrails(guardrails)}

How to work:
1. Read ${diffPath} and isolate the hunks belonging to this fix-area.
2. Read the actual files under ${repo} - both the before and after state. Follow callers and callees. Grep for every reader of anything this change writes, and every writer of anything it reads.
3. Hunt specifically for: broken invariants; changed error and edge-case handling; off-by-one and boundary shifts; sign, unit, and rounding changes; null or undefined newly reachable; ordering and idempotency breaks; partial-failure and retry behaviour; races and changed lock scope; identity or permission checks now bypassable; resource leaks; and callers this change silently invalidated.

Rules that decide whether your output is worth anything:
- Ground every finding in the real repo. Cite file:line for the offending line and for every hop of the code path.
- Give a CONCRETE trigger: the specific inputs and starting state that reach the bug, then what the system does wrong.
- Only report regressions this diff introduces or newly exposes. Behaviour that was already broken before the diff does not count here.
- No speculation, no "could potentially", no style opinions, no missing-test complaints.
- An empty findings array is a correct and respected answer. A fabricated finding is worse than none: an independent verifier will read your code path and refute it.
- Report at most ${maxFindings} findings; if you have more, keep the most severe.
- Set confidence honestly - 0.9 or above only when you traced the whole path yourself, 0.5 or below when any hop is assumed.`
}

function verifyPrompt(repo, diffPath, guardrails, area, finding, lens) {
  return `You are an INDEPENDENT verifier. Another agent claims it found a regression. Your job is to REFUTE it.

Your default verdict is REFUTED. You may return refuted=false only if you can, yourself, point to (a) the exact code path in the real repo and (b) a concrete trigger that reproduces the wrong behaviour. If you cannot produce both from your own reading, the verdict is refuted.

Repo: ${repo}
Diff: ${diffPath}

${describeArea(area)}

Project guardrails:
${describeGuardrails(guardrails)}

The claim under test:
${describeFinding(finding)}

Your assigned lens - ${lens.id}:
${lens.instruction}

How to work:
- Do not trust the claim's citations. Open the files under ${repo} and read them yourself; its line numbers may be wrong or invented.
- Read ${diffPath} to see what this change actually did, versus what the claim says it did.
- Valid refutations include: the described code path does not exist; the trigger cannot occur; an upstream guard prevents it; the cited guardrail says something else; and the behaviour is pre-existing rather than introduced by this diff.
- Do not refute on tone, on severity nitpicks, or because the bug seems unlikely in practice. Refute on facts.

Return refuted, a one-paragraph reason, evidence as file:line references you read yourself, and a counterExample (the code or input that defeats the claim, or "" if you did not refute it).`
}

// Stage helpers

async function verifyOneFinding(repo, diffPath, guardrails, area, finding, verifierCount) {
  const thunks = []
  for (let i = 0; i < verifierCount; i += 1) {
    const lens = VERIFIER_LENSES[i % VERIFIER_LENSES.length]
    thunks.push(() => agent(verifyPrompt(repo, diffPath, guardrails, area, finding, lens), {
      label: `verify ${finding.id} / ${lens.id}`,
      phase: 'Verify',
      schema: SCHEMA_VERDICT,
      effort: 'high',
    }))
  }

  const raw = await parallel(thunks)
  const verdicts = []
  for (let i = 0; i < raw.length; i += 1) {
    if (!raw[i]) continue
    verdicts.push({
      lens: VERIFIER_LENSES[i % VERIFIER_LENSES.length].id,
      refuted: raw[i].refuted === true,
      reason: raw[i].reason,
      evidence: raw[i].evidence,
      counterExample: raw[i].counterExample,
    })
  }

  return {
    ...finding,
    verdicts,
    verdictsReturned: verdicts.length,
    verifiersRequested: verifierCount,
    confirmed: isConfirmed(verdicts),
  }
}

// Input normalization

const input = args || {}
const repo = typeof input.repo === 'string' ? input.repo.trim() : ''
const diffPath = typeof input.diffPath === 'string' ? input.diffPath.trim() : ''

if (!repo) throw new Error('diff-regression-hunt: args.repo is required - the absolute path to the repo checkout the diff applies to, e.g. { repo: "/home/me/collabs/acme/repo" }.')
if (!diffPath) throw new Error('diff-regression-hunt: args.diffPath is required - the path to a unified diff file, e.g. { diffPath: "/home/me/collabs/acme/review.diff" }. Produce one with `git diff <base>..<head> > review.diff`.')

const guardrails = toStringList(input.guardrails)
const maxAreas = clampInt(input.maxAreas, 1, 64, DEFAULT_MAX_AREAS)
const maxFindingsPerArea = clampInt(input.maxFindingsPerArea, 1, 64, DEFAULT_MAX_FINDINGS_PER_AREA)
const verifierCount = clampInt(input.verifiers, 1, 5, DEFAULT_VERIFIERS)
const truncated = { areasDropped: [], findingsDropped: [] }

log(`Regression hunt: repo=${repo} diff=${diffPath} guardrails=${guardrails.length} verifiers=${verifierCount}`)

const suppliedAreas = normalizeAreas(input.areas)
if (Array.isArray(input.areas) && suppliedAreas.length === 0) {
  log('args.areas was supplied but held no usable entries - falling back to triage.')
}

// Risk gate - this workflow costs ~10-15 agents, so a typo fix must not pay for it

phase('Triage')

let riskAssessment = null
if (input.force === true) {
  log('args.force === true - skipping the risk classifier and hunting regardless.')
} else {
  riskAssessment = await agent(riskPrompt(repo, diffPath, suppliedAreas), {
    label: 'risk classifier',
    phase: 'Triage',
    schema: SCHEMA_RISK,
    model: 'haiku',
    effort: 'low',
  })

  if (!riskAssessment) {
    // Failing open is the safe direction here: a dead classifier must not silently
    // cancel the review of a diff that might move money.
    log('Risk classifier returned nothing - proceeding with the hunt rather than skipping it.')
  } else {
    const categories = toStringList(riskAssessment.categories)
    const flaggedAreas = toStringList(riskAssessment.flaggedAreas)
    if (riskAssessment.risk === 'low' && categories.length === 0 && flaggedAreas.length === 0) {
      const reason = riskAssessment.reason || 'No money / safety / data-integrity / auth / concurrency surface found in this diff.'
      log(`Low risk, no area flagged - skipping the hunt. ${reason}`)
      log('Pass args.force === true to run it anyway.')
      return { skipped: true, reason, riskAssessment, confirmed: [], refuted: [] }
    }
    log(`Risk=${riskAssessment.risk} categories=[${categories.join(', ') || 'none'}] flagged=[${flaggedAreas.join(', ') || 'none'}] - proceeding.`)
  }
}

// Fix-areas

let areas = suppliedAreas
if (areas.length === 0) {
  log('No fix-areas supplied - deriving them from the diff.')
  const triage = await agent(triagePrompt(repo, diffPath, maxAreas), {
    label: 'derive fix-areas',
    phase: 'Triage',
    schema: SCHEMA_AREAS,
    effort: 'medium',
  })
  areas = triage ? normalizeAreas(triage.areas) : []
  if (areas.length === 0) {
    throw new Error(`diff-regression-hunt: could not derive any fix-areas from ${diffPath}. Check that the file exists and is a non-empty unified diff, or pass args.areas explicitly.`)
  }
  log(`Triage derived ${areas.length} fix-area(s): ${areas.map((a) => a.name).join(', ')}`)
}

if (areas.length > maxAreas) {
  truncated.areasDropped = areas.slice(maxAreas).map((a) => a.name)
  log(`CAPPED: ${truncated.areasDropped.length} fix-area(s) dropped by maxAreas=${maxAreas} and NOT probed: ${truncated.areasDropped.join(', ')}`)
  areas = areas.slice(0, maxAreas)
}

// Probe -> Verify. pipeline(), not parallel() then parallel(): there is no barrier
// between the stages, so verification of area A's findings starts while area B is still
// being probed. One slow breaker cannot stall verification of every other area.

const pipelineResults = await pipeline(
  areas,

  async (_prev, area) => {
    const probe = await agent(probePrompt(repo, diffPath, guardrails, area, maxFindingsPerArea), {
      label: `probe ${area.name}`,
      phase: 'Probe',
      schema: SCHEMA_FINDINGS,
      effort: 'high',
    })

    if (!probe || !Array.isArray(probe.findings)) {
      log(`Probe ${area.name}: no result.`)
      return { area, findings: [] }
    }

    let findings = probe.findings.filter(Boolean)
    if (findings.length > maxFindingsPerArea) {
      const droppedTitles = findings.slice(maxFindingsPerArea).map((f) => f.title)
      truncated.findingsDropped.push({ area: area.name, dropped: droppedTitles.length, titles: droppedTitles })
      log(`CAPPED: ${area.name} returned ${droppedTitles.length} finding(s) over maxFindingsPerArea=${maxFindingsPerArea}; NOT verified: ${droppedTitles.join(' | ')}`)
      findings = findings.slice(0, maxFindingsPerArea)
    }

    const identified = findings.map((finding, i) => ({ ...finding, area: area.name, id: `${area.name}#${i + 1}` }))
    log(`Probe ${area.name}: ${identified.length} finding(s).`)
    return { area, findings: identified }
  },

  async (probed, area) => {
    // An area with nothing to verify spawns no verifiers at all - this is what keeps a
    // clean diff from paying for the verification half of the workflow.
    if (!probed || probed.findings.length === 0) return { area, verified: [] }

    const verified = await parallel(probed.findings.map(
      (finding) => () => verifyOneFinding(repo, diffPath, guardrails, area, finding, verifierCount),
    ))
    const kept = verified.filter(Boolean)
    log(`Verify ${area.name}: ${kept.filter((f) => f.confirmed).length}/${kept.length} survived refutation.`)
    return { area, verified: kept }
  },
)

// Report

const completed = pipelineResults.filter(Boolean)
const areasProbed = completed.map((result) => result.area.name)
const allVerified = completed.flatMap((result) => result.verified)
const confirmed = allVerified.filter((f) => f.confirmed).sort(compareFindings)
const refuted = allVerified.filter((f) => !f.confirmed).sort(compareFindings)
const stats = { probed: completed.length, found: allVerified.length, confirmed: confirmed.length, refuted: refuted.length }
const base = { skipped: false, repo, diffPath, areasProbed, stats, riskAssessment, truncated }

if (allVerified.length === 0) {
  log(`No findings across ${completed.length} fix-area(s) - no verifiers were spawned.`)
  return { ...base, confirmed: [], refuted: [] }
}

log(`Done: ${stats.confirmed} confirmed, ${stats.refuted} refuted, from ${stats.found} raw finding(s) across ${stats.probed} fix-area(s).`)
for (const finding of confirmed) {
  log(`CONFIRMED [${finding.severity}] ${finding.file}:${finding.line} - ${finding.title}`)
}

return { ...base, confirmed, refuted }
