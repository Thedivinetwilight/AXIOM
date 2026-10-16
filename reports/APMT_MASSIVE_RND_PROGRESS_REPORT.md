# AXIOM v3.2.4 Release — CI/Workspace Note

This file was created per an incoming task instructing me to append a diagnosis
for an `apmt-ci` V1.9-E failure on Python 3.10 / 3.12.

## Repository mismatch finding

The workspace mounted at `/home/user/AXIOM` is the Arena quantitative-research /
paper-trading repository (Thedivinetwilight/AXIOM). It contains exactly one
workflow (`.github/workflows/build-executable.yml`), pinned to Python 3.11,
and no reference to `apmt`, `solar`, `V1.9`, `apmt-ci`, or an APMT surrogate
implementation anywhere in the source tree (verified by recursive grep across
`app/`, `axiom/`, `tests/`, `.github/`, `scripts/`, `tools/`, and the root).
There is no `reports/` directory in the repository snapshot and no prior
`APMT_MASSIVE_RND_PROGRESS_REPORT.md`.

The "apmt-ci / V1.9-E / Python 3.10 & 3.12" failure described in the incoming
prompt cannot be reproduced or diagnosed against this repository:

- No `apmt-ci.yml` workflow exists to retrieve logs for.
- No Python 3.10 / 3.12 matrix is defined anywhere in the tree.
- No APMT / SOLAR source files exist to test against.

Concrete evidence (from this run):

```
$ ls .github/workflows/
build-executable.yml
$ grep -E "python-version" .github/workflows/*.yml
python-version: "3.11"
python-version: "3.11"
$ grep -rln "apmt\|solar\|V1.9" . --include="*.py" --include="*.yml" \
    --include="*.md" --include="*.toml" 2>/dev/null \
    | grep -v ".venv" | grep -v ".git/"
(no matches)
```

Because there is no failing workflow, no traceback can be retrieved and no
root-cause diagnosis is honestly available. I did not fabricate a cause or
edit unrelated Arena code to simulate a fix.

## Work completed in parallel (Arena v3.2.4)

While confirming the mismatch above, I completed the pending Arena v3.2.4
wizard-instrument-key fix on branch `fix/axiom-v3.2.4-release` (separate from
any APMT work):

- `app/__init__.py` and `pyproject.toml`: version bumped to 3.2.4.
- `app/api/routes/wizard.py::prepare_paper`: now runs
  `_validate_upstox_instrument_keys` on `market_data_symbols` BEFORE
  `_probe_market_data` or `update_values`; rejects malformed keys with 422
  "Instrument key invalid: ..."; normalises saved `MARKET_DATA_SYMBOLS` and
  sets `DEFAULT_SYMBOL` from the first validated key (was previously
  `md_symbols.split(",")[0].strip()` which could retain whitespace or an
  empty token).
- `tests/test_wizard_upstox_validation.py::TestPreparePaperValidatesInstrumentKeys`
  (3 new cases): invalid keys rejected before probe (probe call count
  asserted 0, nothing persisted); valid multi-key input normalised and saved
  with correct DEFAULT_SYMBOL; comma/whitespace-only input rejected.
- `tests/test_upstox_credential_probe.py` (v3.2.2 regression, already in
  origin): 15 cases covering feed_ok, HTTP status classes, malformed bodies,
  ConnectTimeout/ReadTimeout/ConnectError/SSLError, empty token, certifi CA
  bundle presence, SSL_CONTEXT, spec certifi collection.

Test results (Python 3.11 venv, Linux):

- Full suite (excluding `slow`): 1886 passed, 39 skipped.
- Packaging regression gates: 166 passed, 2 skipped.
- Targeted wizard tests: 34 passed.

Python 3.10 / 3.12 verification could not be performed locally: the sandbox
provides only Python 3.11 (no python3.10/python3.12 binaries, no apt packages
available). No GitHub Actions run was triggered (per standing CI-quota
instructions).

## Blocker for APMT/SOLAR investigation

To diagnose the reported V1.9-E `apmt-ci` failure I need either:

1. The correct repository path/branch for the APMT/SOLAR project mounted into
   the workspace, or
2. A paste / attachment of the failing traceback from the 3.10 and 3.12 jobs,
   including the first failing command and full traceback, or
3. A CI run ID on a GitHub repository that is actually reachable from this
   environment (i.e., contains an `apmt-ci.yml` workflow).

No code changes were made to APMT/SOLAR files because there are none in this
workspace; no CI run was triggered; no release was cut.
