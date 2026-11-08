# CI and release gates

This page records what the repository's automation actually enforces, what a
green run does and does not prove, and the branch-protection settings an owner
must apply by hand because a workflow token cannot.

> **Nothing here is a release claim.** A passing Linux suite is not Windows
> acceptance, and Windows acceptance is not live-trading readiness. See
> `docs/WINDOWS_ACCEPTANCE_STATUS.md` and `docs/LIVE_EXECUTION_READINESS.md`.

## 1. Workflows

| Workflow | File | Trigger | What it proves |
|---|---|---|---|
| **operator checks** | `.github/workflows/paper-checks.yml` | push to the development branches, PR to `main`, manual | Focused Linux suites (fake MT5, chart, US paper, autonomy), the terminal JavaScript harnesses, and on `full`: the whole Python suite plus `npm test`. |
| **windows release** | `.github/workflows/windows-release.yml` | push to the development branches, manual | Builds the Windows x64 artifacts, runs the Windows-focused suites, the frozen smoke checks, the installer install / upgrade / uninstall acceptance, and the portable acceptance. On manual dispatch with `publish: true` it also creates a prerelease. |

## 2. Why a branch push cannot publish

The prerelease step is gated on both conditions at once:

```yaml
if: success() && github.event_name == 'workflow_dispatch' && inputs.publish
```

A `push` event can therefore never reach `gh release create`, no matter what the
branch is called or what the commit message says. The publish step additionally
refuses to replace an existing tag and verifies, after uploading, that the
downloaded assets match `SHA256SUMS.txt` and that the release target is the
exact commit that was built.

Push runs on the development branches produce **evidence only**: build logs,
`BUILD_METADATA.json`, both test logs, the installer and portable logs, and the
packaging artifacts, retained for 30 days.

## 3. Focused-test paths must exist

Both `.github/workflows/paper-checks.yml` and
`packaging/windows/build_windows.ps1` name explicit test files. A single stale
path fails the whole gate: pytest exits non-zero when asked for a file that does
not exist, so the "focused" step reds out before running a single assertion.

That is exactly what happened while `tests/test_vantage_api.py` was listed after
the live Vantage API was removed. The file is gone; its intent is preserved by
`tests/test_vantage_live_api_removal.py`, which asserts the removed routes still
answer `404` and that nothing re-mounts them, alongside
`tests/test_vantage_ui_removal.py` and the connector suite.

`tests/test_start_monitor_launchpad.py` now pins this statically:

* every `tests/...py` path named by either gate must exist;
* `tests/test_vantage_api.py` must not reappear;
* any gate that runs the Vantage connector suite must also run the removal
  contract.

## 4. Windows acceptance is a separate stage from the focused suites

A successful "focused tests" step is **not** Windows acceptance. Acceptance is
reached only when all of these have run on `windows-latest`:

1. `build_windows.ps1` — focused suites, JavaScript harnesses, frozen onefile and
   onedir smoke checks, PyInstaller freeze, installer build.
2. **Verify the produced artifacts** — every artifact's byte length and SHA-256
   against `BUILD_METADATA.json`, plus the eight blocker-fix source hashes.
3. **Install the loaderless installer, launch onedir, upgrade, then uninstall** —
   `install_test.ps1`, which records `PASS`/`FAIL` per phase.
4. **Extract and test the onedir portable ZIP** — `portable_test.ps1` against the
   exact commit.
5. **Finalize install results** — fails if any `FAIL` line exists, or if the
   installed, upgraded or portable "frozen PAPER blocker fixes" diagnostics are
   missing.

The workflow reads its own logs and throws on a missing phase, so a green job
means those stages ran — not merely that the focused step did.

## 5. Branch protection on `main` — owner action required

At the time of writing, `main` **is not protected**:
`GET /repos/.../branches/main` reports `"protected": false`, and
`GET /repos/.../rulesets` returns an empty list.

The automated workflow token **cannot fix this**. Every protection and ruleset
call returns HTTP 403 `Resource not accessible by integration`, for reads as
well as writes, even though the repository reports `admin: true` for the
credential. GitHub requires branch-protection administration over an owner
token or through the web UI; a workflow-scoped installation token is refused.

### Recommended settings

Apply to `main` in **Settings → Branches** (or a ruleset targeting `main`):

| Setting | Value |
|---|---|
| Require a pull request before merging | **on**, at least 1 approval |
| Dismiss stale approvals on new commits | on |
| Require status checks to pass | **on** (list below) |
| Require branches to be up to date before merging | on |
| Require conversation resolution | on |
| Require linear history | optional |
| Block force pushes | **on** |
| Restrict who can push (`Do not allow bypassing`) | **on**, including administrators |
| Allow deletions | **off** |

### Required status checks

Use the **job** names, which are stable; the workflow keys the checks, so a
renamed job breaks the rule silently.

| Context | Source workflow | Requirement |
|---|---|---|
| `focused (Vantage fake + chart + US paper)` | operator checks | required |
| `full suite` | operator checks | required |
| `build + install-test (windows x64)` | windows release | required |

The Windows check covers the Windows build/install/upgrade/uninstall and portable
stages, because those all run inside that one job. Do not add the workflow *name*
("operator checks" / "windows release") as well — that would double-count and
make a skipped job look satisfied by the other.

### Development branch

`arena/8d3d005e-axiom` is a development branch and stays separate from release
status. Do not point protection at it; do not merge it into `main` as part of a
release. Release status is decided only by a completed Windows run on an exact
commit and, separately, by the owner's broker checks.

## 6. What is still unverified

* **Branch protection is not applied.** The settings above are a recommendation;
  until an owner applies them, `main` accepts direct pushes and force pushes.
* **Windows acceptance has been observed green once**, on commit
  `c75f46cb32dafce82c8c1a2a8a09066b344a1550` (run `42505119536`): steps 6-10 all
  passed — full Python suite, focused suites, exact-commit benchmarks, artifact
  verification, installer install / upgrade / uninstall acceptance and portable
  ZIP acceptance — and the job published no release. This is a single green run
  on one commit, not a standing guarantee; re-verify on any later commit before
  treating a build as releasable. Live-trading readiness is still separate and
  unverified.
* **No live-trading readiness, profitability, or broker-verification claim** is
  made or implied by any of the above.
