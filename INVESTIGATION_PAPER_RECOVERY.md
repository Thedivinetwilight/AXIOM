# Investigation: Paper-account recovery failure (Windows build flags not present in this source tree)

**Date:** 2026-10-20  
**Branch:** `arena/7087b053-axiom` (base `fix/axiom-v3.2.4-release` @ `4317e3a`)  
**Constraint:** read-only investigation only — no destructive DB commands, no compatibility weakening, no trading-logic changes

## Reported symptom

```
--recover-paper-account --confirm-new-empty-paper-account  → exit 3 (persistent PAPER recovery DB)
--clear-paper-database-override                              → rejected
Listed help shows --clear-paper-recovery  (not --clear-paper-database-override)
```

## Result: flags not implemented in this build

Exhaustive source search on this branch returns **zero definitions** for any of the reported spellings:

```
grep -rn "recover-paper-account|clear-paper-recovery|clear-paper-database-override|confirm-new-empty-paper-account|PAPER.*recovery" repo
→ 0 hits in *.py, *.bat, *.ps1, *.md

grep -rn "paper" packaging/ --include="*.py"   → only paper-live/paper-test/paper engine references (no recovery flag)
```

`app/__main__.py:1730 parser()` (1,982-line file) enumerates every real subcommand:

- `upstox-test`, `market-stream`, `paper-live`, `paper-test`, `upstox-login`, `engine`, `live-preflight/status/halt/resume`, `sandbox-test`, `initialize`, `market-universe`, `history`, `dataset`, `universe`, `research`, `report`, `maintenance`, `ask`, `scan`, `discover`, `lifecycle`, `replay`, `monitor`, `backup`

No `recover-paper*` or `clear-paper*` subparser/argument exists. The `arena.py` wrapper (frozen entry) likewise only proxies `--cli`.

**Conclusion:** the exit-3 / unknown-argument behaviour originates **outside this source tree** — Windows packaged executable wrapper, out-of-date docs vs code, or a different branch's CLI. This repo's CLI does not know how to "recover" or "clear" a paper recovery DB because it does not maintain a separately-selected paper recovery database.

## What this build actually does with paper state

| Location | Role | Verbatim evidence |
|---|---|---|
| `app/runtime.py:40 arena_home()` | Resolves storage home (`%LOCALAPPDATA%\Arena` when frozen, else repo root). | `def arena_home():` + frozen branch |
| `app/runtime.py:default_database_path()` | Returns absolute `sqlite:///<home>/trading_bot.db` | `return f"sqlite:///{(root / 'trading_bot.db').as_posix()}"` |
| `app/runtime.py:env_file()` , `env_candidates()` | Portable `.env` lookup order | documented in-file |
| `run.py:parser()` `--mode paper\|backtest\|...` | Paper pipeline entry (not `app/__main__.py`). Uses `Database(settings.database_url)` / `Repository` / `PaperBroker`/`PaperSimulator` | `from app.paper_trading.broker import PaperBroker` |
| `app/paper_trading/broker.py:49 PaperBroker.__init__` | Loads persisted paper state (`repository.load_broker()`) if present, validates `config/currency/timeframe/calendar` strictly; on mismatch raises `ValueError("Saved paper account config differs; use a new database")` and `ValueError("Saved paper calendar differs; use a new database")`. | `state.get("calendar") != calendar_signature(self.calendar)` etc. |
| `app/paper_trading/account.py:PaperAccount` | Portfolio wrapper; no recovery logic | class line 4 |
| `app/database/database.py:20 Database.__init__` | Applies any **staged** restore *before* opening SQLite: `apply_pending_restore(self.path)` | `restore = apply_pending_restore(self.path)` |
| `app/database/repository.py:91 load_broker()` / `98 save_broker()` | Single `state` table key `'paper'` holds `broker.dump()` JSON atomically | `SELECT payload FROM state WHERE key='paper'` |
| `app/system/backup.py:BackupManager` + `apply_pending_restore()` | Tar+`manifest.json` SHA-256 archive, `verify()` (integrity + CORE_TABLES counts), staged restore (`<db>.restore-pending`) requiring restart; `verify`→`restore` never deletes in place without a verified archive | `def verify`, `def restore`, `def apply_pending_restore` at line 618 |
| `app/database/recovery.py:RecoveryManager` | SQLite online-backup API `backup`/`verify`/`restore` via `app __main__ backup` command | `class RecoveryManager` |
| `app/__main__.py:1930 backup` subcommand | Only official backup/restore CLI: `python -m app backup backup|verify|restore --path ...` | `bak.add_argument("action", choices=["backup","verify","restore"])` |
| `app/bootstrap.py:initialize()` | Idempotent first-run setup (`python -m app initialize`) creating dirs + DB migrations | `def initialize(settings)` |

**No persistent "PAPER recovery database selection" state** exists in these files. There is no `*-recovery.db` sentinel written by this code. Any `*-recovery.db` or `-recovery.db` file alongside the DB on Windows is either operator-made or from the external wrapper; this source never reads it to decide which DB to use.

## Why `exit 3` cannot be from this code

- `run.py:main()` catches `ValueError/OSError/RuntimeError → exit 1`, unknown modes → exit 2.
- `app/__main__.py` subcommands return 0/1 (2 for argument errors via argparse). No path returns 3.
- An `argparse` unknown argument prints `unrecognized arguments` to stderr and exits **2**, not 3.

Exit 3 must be produced by the external launcher/exe that implements the reported `recover-paper*` flags.

## Exact safe recovery procedure for *this* build (no data loss)

**Do NOT run the reported flags** — they do not exist here and cannot fix this DB.

1. **Locate the single source of truth (your current DB):**
   ```powershell
   python -c "from app.runtime import arena_home, env_file; print(arena_home()); print(env_file())"
   # or explicitly:
   python -c "from app.runtime import arena_home; from pathlib import Path; print((arena_home()/\"trading_bot.db\").resolve())"
   ```
   Also check any explicit override: `echo $env:DATABASE_URL` (PowerShell) / `%DATABASE_URL%` (cmd).

2. **Stop Arena completely** (close all terminals running `arena.py` / the .exe; confirm no `Arena.exe` in Task Manager). Leave nothing holding `trading_bot.db`.

3. **Preserve current state** (do not delete yet):
   ```powershell
   $home = python -c "from app.runtime import arena_home; print(arena_home())"
   Copy-Item "$home\trading_bot.db" "$home\trading_bot.db.pre-$(Get-Date -Format yyyyMMdd-HHmmss).bak" -ErrorAction Ignore
   Copy-Item "$home\trading_bot.db-wal" "$home\trading_bot.db-wal.pre-$(Get-Date -Format yyyyMMdd-HHmmss).bak" -ErrorAction Ignore
   Copy-Item "$home\trading_bot.db-shm" "$home\trading_bot.db-shm.pre-$(Get-Date -Format yyyyMMdd-HHmmss).bak" -ErrorAction Ignore
   # also preserve any wrapper-created recovery files if present:
   Get-ChildItem "$home\*-recovery.db" -ErrorAction Ignore | ForEach-Object { Copy-Item $_.FullName "$($_.FullName).bak" }
   ```

4. **Create a new empty PAPER account** — one of:

   **A. Recommended (explicit, idempotent):**
   ```powershell
   Remove-Item "$home\trading_bot.db","$home\trading_bot.db-wal","$home\trading_bot.db-shm" -Force -ErrorAction Ignore
   python -m app initialize
   ```
   `initialize` recreates migrations, inserts the explicit `engine_state(emergency_stop=false)` bootstrap row for a genuinely new DB, and reports `schema_versions`.

   **B. Minimal (let first paper run recreate):**
   ```powershell
   Remove-Item "$home\trading_bot.db","$home\trading_bot.db-wal","$home\trading_bot.db-shm" -Force -ErrorAction Ignore
   python -m app paper-test --database "$home\trading_bot.db"   # offline smoke test; proves DB is writable and empty
   ```

   **C. Preserve original file untouched, use a fresh DB path via env (useful when diagnosing):**
   ```powershell
   $env:ARENA_HOME="C:\Temp\ArenaFresh"
   python -m app initialize
   python -m app paper-test --database "$env:ARENA_HOME\trading_bot.db"
   ```

5. **Verify:**
   ```powershell
   python -m app backup verify --path "$home\backups\<latest>.tar.gz"   # if you have archives
   python arena.py --diagnostics  2>&1 | Select-String "arena_home|database|error" -CaseSensitive:$false
   ```

6. **If you hit `Saved paper account config differs; use a new database`** — this is `app/paper_trading/broker.py:88` firing because the stored `ExecutionConfig/currency/timeframe/calendar` snapshot does not match the current `.env`/code. That message's advice is literal: a *new* (empty) `trading_bot.db` is the intended fix, exactly as in step 4. Do not attempt to mutate the stored blob.

7. **If recovery was attempted via `backup restore` and is pending:** `app/system/backup.py` stages to `<db>.restore-pending` and requires a restart before SQLite opens the DB. Either restart Arena to apply it, or delete `<db>.restore-pending` to abandon it — the active DB is untouched until restart.

## What NOT to do

- Do not delete `*-recovery.db` hoping it "clears the selection" — this code does not use it, and on Windows it may be your only backup copy.
- Do not edit `trading_bot.db` with external SQLite tools while Arena holds `*.paper.lock` / `live.lock` — writer-guard (`Repository.acquire_writer`) will refuse and may leave WAL sidecars in an inconsistent state.
- Do not weaken the `broker.py:82/88` config-equality check to force-recover a paper session after changing `DEFAULT_TIMEFRAME`/`currency`/`execution_kwargs`/`market_calendar` — create a new DB instead (this preserves reproducibility of backtests and avoids silent leverage/cost mismatches).

## Evidence of no modifications

Working tree remains clean except this report (intentionally untracked to preserve audit history):

```
git status --porcelain → no staged changes
grep -rn flags → 0 hits (documented above)
No sqlite commands executed; no file deletions performed.
```

## Forward action for the Windows exe

Until the packaged CLI is rebuilt from this source (or its wrapper flags are removed from docs), treat any `--recover-paper-account` / `--clear-paper*` documentation as stale. The canonical operators for this build are `python -m app initialize` and `python -m app backup verify|restore --path <archive>`. A follow-on PR that intentionally adds a `python -m app paper reset --confirm <phrase>` safe-reset command would be appropriate, but must be a separate change with explicit spec — not inferred from the reported flags.
