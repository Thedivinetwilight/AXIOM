# AXIOM — Final Known Issues

**Date:** 2026-10-19 · **Verified code commit:** `002da1198312a2def2d31a6368729ff91da59d78`
**Rule:** a mock is a mock, a skipped test is not a pass, a source test is not a
packaged-executable test, login is not order safety, and a backtest is not a
profitability claim.

| ID | Area | Issue | Status | Why it is not fixed here |
|---|---|---|---|---|
| K-01 | Packaging | No one-file executable was built in this environment | **BLOCKED** | The container Python has no shared `libpython3.11`; no dev package offline; the standalone-interpreter release asset is outside allowed egress. Fix requires a machine with a shared Python (or GitHub Actions). |
| K-02 | Packaging | Frozen end-to-end gate not run against a real artifact | **UNVERIFIED** | Depends on K-01. The push-triggered CI run passed the Ubuntu source suite only; dispatching the three-OS matrix failed with `HTTP 403` (session token lacks `actions:write`). |
| K-03 | Packaging | Windows and macOS executables | **UNVERIFIED** | No such machines here; CI matrix requires `workflow_dispatch`/tag and was not dispatched for all three OSes in this session. |
| K-04 | Packaging | PySide6 desktop executable | **UNVERIFIED** | PySide6 is not installed, there is no display server, and no desktop bundle exists. Only viewmodels/import contracts are tested. |
| K-05 | External | Real Upstox market-data feed (`wss://`) | **BLOCKED** | `api.upstox.com` TLS is refused by sandbox egress (`SSL_ERROR_SYSCALL`) and no credentials exist. |
| K-06 | External | Real Upstox historical/intraday download | **BLOCKED** | Same egress block; URL construction and retry policy are unit-tested only. |
| K-07 | External | Real OAuth authorization-code flow | **BLOCKED** | No client id/secret, no registered redirect, no reachable token endpoint. Flow is implemented and covered by unit tests with injected transports. |
| K-08 | External | Real sandbox order / live account read | **BLOCKED** | No sandbox or live credentials; mission forbids any real order. |
| K-09 | External | Live order placement safety in practice | **BLOCKED / NOT TESTED** | The gateway, guard chain and emergency stop are software contracts. No order was ever sent to a real exchange; claims are limited to the in-process boundary. |
| K-10 | External | Kronos genuine pretrained inference | **BLOCKED** | No checkout (`AXIOM_KRONOS_SOURCE`), no genuine weights, no pandas/torch. 26 skips document this; random weights were never substituted. |
| K-11 | External | GPU/CUDA forecast runtime | **BLOCKED** | No CUDA or torch in the environment; unverified. |
| K-12 | External | Public Upstox instrument master download | **BLOCKED** | `assets.upstox.com` TLS refused; instrument schema is exercised against fixtures only. |
| K-13 | Safety model | Out-of-band HTTP by a process that already holds a broker token | **NOT APPLICABLE** | Outside any in-process control; documented as an explicit architectural limit, not silently ignored. |
| K-14 | Calendars | `nse-session` with `DEFAULT_TIMEFRAME=1D` has no adapter | **PARTIAL** | Constructor accepts it so an existing config is not turned into a generic failure; the engine preflight and the `engine` command refuse it with a specific message. Choosing a calendar-by-timeframe matrix is a product decision, not a repair. |
| K-15 | Paper account | Recovered account starts empty | **EXPECTED** | `--new-paper-account` deliberately does not migrate history; the old database is preserved and named in the output. There is no merge/import tool. |
| K-16 | Diagnostics | `arena.py --diagnostics` exits 0 for `MISMATCH`/`UNREADABLE` | **EXPECTED** | Diagnostics are informational by contract; the launcher refuses with exit 3/4. Automation must read the JSON status, not the exit code. |
| K-17 | Tests | 39 skipped tests | **UNVERIFIED for their real subjects** | Optional dependencies and external artifacts are absent; each skip reason is listed in `AXIOM_FINAL_RUN_LOG.md`. They are not counted as passes. |
| K-18 | Tests | Source suite only, single OS | **PARTIAL** | 1926 tests pass on Linux/Python 3.11. The Windows portability suite is static analysis; real Windows behaviour is CI-gated. |
| K-19 | Research | Backtests, walk-forward and lifecycle results | **NOT A PROFITABILITY CLAIM** | They verify software contracts on controlled/local data. No statistical edge, out-of-sample significance or regulatory suitability is claimed. |
| K-20 | Repository | Base checkout had a single squashed commit; prompt-referenced SHAs/branch/files absent | **DISCREPANCY RECORDED** | `e983633`, `f8d1070`, `14a5602`, `e7aa126`, branch `arena/c292d276-axiom` and `LIVE_TRADING_SWITCH_IMPLEMENTATION_REPORT.md` do not exist here. No work was fabricated to match them. |
| K-21 | Docs | `LIVE_TRADING_ENABLEMENT.md` and the Upstox 3.0.5 guide remain long historical documents | **PARTIAL** | Superseding banners mark the contradicted sections; rewriting 900 lines of design history was out of scope and would reduce traceability. |
| K-22 | CI | Push-triggered workflow runs only the Ubuntu source suite | **PARTIAL** | Run [#39262011293](https://github.com/Thedivinetwilight/AXIOM/actions/runs/39262011293) passed. The three-OS matrix is `workflow_dispatch`/tag-only and could not be dispatched from this session (403); a green push run is **not** artifact evidence. |
| K-23 | Upstox streaming | Retry cap is 25 consecutive failures | **EXPECTED** | After the cap the feed stops with `UNAVAILABLE` instead of reconnecting forever; the operator must restart. This is fail-closed by design. |
| K-24 | Upstox streaming | A malformed authorize 200 is treated as permanent | **EXPECTED** | Retrying an intercepted/rewritten response would hide a possible MITM. The operator must investigate. |
| K-25 | Tooling | `test_backtest_writes_where_it_says_it_did` is order-sensitive to process-environment mutation | **FIXED AT SOURCE** | The wizard no longer writes `os.environ`; the underlying coupling (a subprocess inheriting a mutated environment) would reappear if process-env mutation is reintroduced. Documented to prevent regression. |

## Items explicitly *not* claimed

- Production readiness, legal/regulatory approval, profitability, or a signed
  release.
- Any real Upstox account state, token, OAuth completion, static-IP
  registration or third-party availability.
- A locally built executable, or a successful three-OS packaging run.
- Genuine Kronos weights, publisher authenticity, forecast quality or GPU
  performance.
