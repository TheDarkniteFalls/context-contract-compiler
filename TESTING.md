# Testing Context Contract Compiler

Context Contract Compiler is a dependency-free local Python tool. Tests use synthetic data
and make no model or external network calls.

## Runtime and Platforms

- Python 3.9 or newer
- Python's `sqlite3` module compiled with FTS5
- A current desktop or mobile browser for the Context Debugger

The current implementation was validated locally with the macOS system Python 3.9.
The checked-in GitHub Actions job targets current Ubuntu and Python 3.x, but it
cannot run against uncommitted changes. Windows is expected to work with a
standard FTS5-enabled Python build but has not been validated for this release.

Check FTS5 before doing anything else:

```sh
python3 -c "import sqlite3; db=sqlite3.connect(':memory:'); db.execute('CREATE VIRTUAL TABLE probe USING fts5(text)'); print('FTS5 ready')"
```

Expected output:

```text
FTS5 ready
```

## One-Command Full Check

From the repository root:

```sh
python3 -B context_compiler.py check
```

This runs the Context Contract Compiler self-test, retrieval-foundation self-test, typed
failure registry, all unit tests, and Python compilation in an isolated cache
directory. Expected final line:

```text
PASS Context Contract Compiler full check
```

A managed environment may deny local socket binding and explicitly skip the
HTTP test class. That is not a passing HTTP result: rerun the unit suite on a
normal local host and require all 39 tests to pass before release.

## Manual Acceptance Route

Start the local debugger:

```sh
python3 -B context_compiler.py serve --open
```

The server prints `http://localhost:8765/` and remains in the foreground.
Press `Ctrl-C` to stop it.

The default result should show:

- header state `COMPILED`;
- naïve packet: 5 selected, 90/96 tokens, 5 illegal, 1 required missed;
- Context Contract Compiler packet: `ADR-0234`, `DEC-0421`, `GUIDE-0112`, `RUN-0880`;
- Context Contract Compiler accounting: 4 selected, 74/96 tokens, 22 remaining;
- trace: 13 decisions, 9 exclusions; and
- deterministic receipt `cg-ff514334694491b7` for the unmodified fixture;
- contract fingerprint `cgc-51acadc4b905a993`; and
- packet fingerprint `cgp-5c8a7d6b68a24f89`.

## Agent Fire Drill Manual Route

Keep the default compiled packet and controls, then:

1. Confirm the drill shows `RECEIPT FROZEN — CURRENT` and
   `cg-ff514334694491b7` as both frozen and current.
2. Select **Inject late correction**. The synthetic user correction makes
   connection-pool stability and `RUN-0880` required context.
3. Require `STALE — RECOMPILE REQUIRED`, outcome `recompile_required`, reason
   `STALE_CONTRACT_FINGERPRINT`, and an unavailable continuation path.
4. Select **Recompile current context**. Require `RUN-0880` to carry the
   `REQUIRED` badge, receipt `cg-c882f867503fbdb4`, and
   `CURRENT — NEW RECEIPT`.

The late contract fingerprint is `cgc-5d044d511e24fa1c`; its packet
fingerprint is `cgp-4ee52d3f972f66bb`. The record set still fits 74/96 tokens,
but the required role and packet order change. This drill is separate from the
seven pre-compilation poison controls.

## Seven-Control Manual Matrix

Exercise every control before release. Restore the default contract between
rows when a control changes the budget.

| Control | Expected visible result |
| --- | --- |
| Future reveal | `FR-1050` is present and traced as `EXCLUDE_FUTURE`; turning it off removes that candidate |
| Superseded decision | `DEC-0410` is traced as `EXCLUDE_SUPERSEDED`; turning it off removes that candidate |
| Generated draft | `DRAFT-0702` is traced as `EXCLUDE_SOURCE`; turning it off removes that candidate |
| Rejected plan | `PLAN-0901` is traced as `EXCLUDE_LIFECYCLE`; turning it off removes that candidate |
| Ambiguous identity | `AMB-0606` is traced as `EXCLUDE_IDENTITY`; turning it off removes that candidate |
| Remove required provenance | Header becomes `FAIL CLOSED`; code is `REQUIRED_MISSING_PROVENANCE`; packet is empty |
| Tight token budget | Budget becomes 12; header becomes `FAIL CLOSED`; code is `REQUIRED_OVER_BUDGET`; packet is empty |

Also verify:

1. restore the two fail-closed controls and recompile successfully;
2. **Copy packet** reports success and clipboard text contains stable IDs,
   provenance, and token accounting;
3. Contract, Compare, and Trace tabs work at a narrow/mobile viewport;
4. every candidate remains reachable in the trace; and
5. the browser console has no errors.

## Direct CLI Checks

Human-readable default compile:

```sh
python3 -B context_compiler.py compile
```

Machine-readable receipt:

```sh
python3 -B context_compiler.py compile --json
```

Expected fail-closed commands return exit code `2`:

```sh
python3 -B context_compiler.py compile --remove-required-provenance
python3 -B context_compiler.py compile --tight-token-budget
```

Remove the five record-poison candidates without changing the other records:

```sh
python3 -B context_compiler.py compile --no-poisons --json
```

## Foundation Regression Commands

The Context Contract Compiler and debugger must not weaken the original retrieval
harness:

```sh
python3 -B metadata_retrieval_demo.py compare
python3 -B metadata_retrieval_demo.py ablate
python3 -B metadata_retrieval_demo.py buckets
python3 -B metadata_retrieval_demo.py failures
python3 -B metadata_retrieval_demo.py --self-test
python3 -B -m unittest discover -s tests -v
```

## API Smoke Test

With the server running in another terminal:

```sh
curl --fail --silent http://localhost:8765/api/health
curl --fail --silent http://localhost:8765/api/compile
```

The health endpoint returns
`{"product":"Context Contract Compiler","status":"ok"}`
(JSON key order is not a contract). The compile endpoint returns the same
deterministic receipt shown in the interface. `POST /api/fire-drill` accepts
exactly `prior_receipt`, `change`, `current_contract`, and `controls`; it
returns `continue`, `recompile_required`, or `block` plus `can_continue`, a
stable reason code, boundary fact, mismatches, and the current proof. Requests
are stateless.

## Public-Safety and Repository Checks

Before any approved commit or push, run:

```sh
git diff --check
python3 -B ../scripts/publicctl.py check .
```

A clean result is evidence for review, not permission to publish. Inspect the
complete diff, verify that every fixture is synthetic, and confirm that no
local paths, credentials, connector data, private source text, or generated
cache files are present.

## Proof Boundary

Tests prove deterministic contract enforcement over supplied structured
metadata and fixtures. They do not prove record truth, metadata completeness,
correct upstream authorization, safe downstream model behavior, privacy,
security, or production readiness. Staleness tests compare supplied receipts,
fingerprints, changes, contracts, and records; they do not observe a real agent
runtime or prove that every real-world change was captured.
