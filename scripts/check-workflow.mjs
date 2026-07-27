#!/usr/bin/env node
/**
 * Syntax-check every `tools/*.workflow.js` — and prove the check still works.
 *
 * Why this is not just `node --check`
 * -----------------------------------
 * A Workflow script is not a standalone Node module. It uses ESM `export` for
 * its `meta` block AND top-level `return` to yield its result, because the
 * Workflow runtime evaluates the file body inside a function. `export` is
 * illegal in CommonJS; top-level `return` is illegal in an ES module. So the
 * file parses as neither, and checking it as-is is not merely unhelpful:
 *
 *     $ node --check tools/diff-regression-hunt.workflow.js   # exit 0
 *     $ printf 'const OOPS = = 1\n' >> copy.js
 *     $ node --check copy.js                                  # exit 0  (!!)
 *
 * Measured on Node v25.2.0: once a file contains a top-level `export`, node's
 * module-detection retry swallows the syntax error and reports success. A naive
 * CI step therefore proves nothing while looking green forever.
 *
 * What this does instead: reproduce the runtime's shape (strip the `export`
 * keywords, wrap the body in an async function), then `--check` that. Every
 * line of real code still goes through the parser.
 *
 * And then it checks the checker. For each file it also builds a deliberately
 * broken copy and asserts the check REJECTS it. If that ever passes, the gate
 * has gone inert and this script fails loudly rather than continuing to report
 * a green tick that means nothing.
 *
 * Zero dependencies, Node built-ins only.
 */

import { execFileSync } from 'node:child_process'
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const WORKFLOW_DIR = join(REPO_ROOT, 'tools')
const WORKFLOW_SUFFIX = '.workflow.js'

// A top-level statement that cannot parse under any grammar. Appended at the
// end so it lands at top level rather than inside a template literal — an
// injection *inside* a template literal is valid JS and would silently make the
// negative control useless.
const POISON = '\nconst __DELIBERATELY_BROKEN__ = = 1\n'

/** Reproduce the shape the Workflow runtime evaluates. */
function toCheckableForm(source) {
  const body = source.replace(/^export\s+(default\s+)?/gm, '')
  return `export default async function __syntaxCheck__() {\n${body}\n}\n`
}

/** Run `node --check` on some source. Returns {ok, output}. */
function nodeCheck(source, scratch, label) {
  const file = join(scratch, `${label}.mjs`)
  writeFileSync(file, source, 'utf8')
  try {
    execFileSync(process.execPath, ['--check', file], { stdio: 'pipe' })
    return { ok: true, output: '' }
  } catch (error) {
    const stderr = error.stderr ? error.stderr.toString() : String(error)
    return { ok: false, output: stderr.trim() }
  }
}

function findWorkflows() {
  let entries
  try {
    entries = readdirSync(WORKFLOW_DIR)
  } catch (error) {
    fail(`cannot read ${WORKFLOW_DIR}: ${error.message}`)
  }
  return entries.filter((name) => name.endsWith(WORKFLOW_SUFFIX)).sort()
}

function fail(message) {
  process.stderr.write(`check-workflow: ${message}\n`)
  process.exit(1)
}

function main() {
  const workflows = findWorkflows()
  if (workflows.length === 0) {
    // Not "nothing to do" — the repo is supposed to contain one. A silent pass
    // here would hide a deleted or renamed file.
    fail(`no *${WORKFLOW_SUFFIX} files found in ${WORKFLOW_DIR}`)
  }

  const scratch = mkdtempSync(join(tmpdir(), 'collab-kit-check-'))
  let failures = 0

  try {
    for (const name of workflows) {
      const source = readFileSync(join(WORKFLOW_DIR, name), 'utf8')
      const label = basename(name, WORKFLOW_SUFFIX)

      const real = nodeCheck(toCheckableForm(source), scratch, `${label}.real`)
      if (!real.ok) {
        process.stderr.write(`FAIL  ${name}\n${real.output}\n`)
        failures += 1
        continue
      }

      // The check must be capable of failing, or its success is meaningless.
      const control = nodeCheck(toCheckableForm(source + POISON), scratch, `${label}.control`)
      if (control.ok) {
        process.stderr.write(
          `FAIL  ${name}: the syntax check accepted deliberately broken code.\n` +
            '      The gate is inert — node accepted a top-level `const x = = 1`.\n' +
            '      Fix toCheckableForm() before trusting any green result here.\n',
        )
        failures += 1
        continue
      }

      process.stdout.write(`ok    ${name} (parsed; negative control rejected)\n`)
    }
  } finally {
    rmSync(scratch, { recursive: true, force: true })
  }

  if (failures > 0) {
    fail(`${failures} of ${workflows.length} workflow file(s) failed`)
  }
  process.stdout.write(`\n${workflows.length} workflow file(s) checked with node ${process.version}\n`)
}

main()
