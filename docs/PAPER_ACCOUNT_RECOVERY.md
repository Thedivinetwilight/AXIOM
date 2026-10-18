# Paper-account startup mismatch and recovery

## What the startup error means

Arena refuses to restore a saved paper account if the assumptions used for its
orders/fills no longer match the current process. The strict comparison covers
all eight `ExecutionConfig` fields (`initial_capital`, `commission`, `slippage`,
`spread`, `position_size`, `stop_loss`, `take_profit`, and `max_participation`),
plus `currency`, `timeframe`, and every field in the calendar signature.
Integer and float JSON representations of the same number are treated as the
same setting; numeric strings, missing fields, unknown execution fields, and
calendar changes remain incompatible.

A refusal now reports only the differing field names and the resolved SQLite
file path. It never logs saved/current account values, orders, fills, balances,
or credentials. The startup check is read-only and runs before SQLite schema
bootstrap. It does not reset or migrate the saved paper account.

The current source defaults are `initial_capital=100000`, `commission=0.0005`,
`slippage=0.0005`, `spread=0.0002`, `position_size=0.25`, `stop_loss=0.05`,
`take_profit=0.10`, and `max_participation=0.10`; the other paper defaults are
`currency=INR`, `timeframe=1D`, and a continuous calendar. These are reference
defaults only: process variables and dotenv files can select other values.
JSON `100000` and `100000.0` are numerically equivalent, while a changed number
or a numeric string is incompatible.

The Windows account/database was not available to this source change, so a
specific historical mismatch should not be inferred. The next refusal will
identify the exact field names for the database that executable actually
selected.

## Find the actual database and dotenv inputs

From PowerShell, run the installed executable with:

```powershell
& "$env:ProgramFiles\AXIOM\Arena.exe" --diagnostics
```

The report contains the resolved `database_path`, `database_url_source`, the
`.env` candidates that exist, their precedence, and the safe execution-mode
summary. It does not print dotenv values or credentials. The durable startup
log is under `<ARENA_HOME>\logs\arena.log` (normally
`%LOCALAPPDATA%\Arena\logs\arena.log`).

For a frozen install, if `DATABASE_URL` is unset, the default database is
`<ARENA_HOME>\trading_bot.db`; the default home is `%LOCALAPPDATA%\Arena` on
Windows and can be changed with `ARENA_HOME`. A configured relative
`DATABASE_URL` is resolved against the process working directory, as SQLite
would open it; diagnostics report the absolute resolved file so a double-click
or installer path is not guessed.

Settings load dotenv files with `override=False`. Effective precedence is:

1. inherited process environment (highest priority);
2. `ARENA_ENV_FILE`, when set; otherwise `<ARENA_HOME>\.env`;
3. `.env` beside `Arena.exe`;
4. the bundled/repository-root `.env`.

The first file in that order wins for duplicate keys, while process environment
always wins over every file. The installer places only safe fallback defaults
beside its executable (`MODE=PAPER`, `LIVE_TRADING=false`, `BROKER=LOCAL_PAPER`);
that file is behind the per-user configuration. The setup wizard writes to the
per-user Arena home, not into Program Files.

## Explicit recovery to a separate empty paper database

Only use this after the error identifies an incompatible saved account. Close
other Arena instances, then run the explicit confirmation command:

```powershell
& "$env:ProgramFiles\AXIOM\Arena.exe" `
  --recover-paper-account --confirm-new-empty-paper-account
```

For a portable install, substitute its actual `Arena.exe` path. From source,
use `python arena.py` instead of the executable path.

The command:

- rechecks that the configured database is genuinely incompatible;
- refuses if it cannot inspect the database safely or another Arena instance
  owns the data directory;
- reserves a new, unique database under
  `%LOCALAPPDATA%\Arena\paper-recovery\` (or the configured `ARENA_HOME`);
- stores a small selection pointer at
  `%LOCALAPPDATA%\Arena\paper-database-override.json`;
- starts Arena with that separate database and keeps the selection on later
  launches.

The new paper database is empty. Old orders, fills, research, and the original
paper-account state are **not copied, overwritten, reset, or automatically
migrated**. They remain in the original database. Recovery is refused unless
execution is still `MODE=PAPER`, `LIVE_TRADING=false`, and
`BROKER=LOCAL_PAPER`; no live execution is enabled and the recovery command
places no orders. A subsequent explicit UI action is still required to create
any paper order.

To remove only the saved selection (without deleting either database), run:

```powershell
& "$env:ProgramFiles\AXIOM\Arena.exe" --clear-paper-database-recovery
```

The next ordinary launch will use the configured `DATABASE_URL`/default again;
if that original account remains incompatible, the strict startup refusal will
appear again. A missing or invalid recovery database never causes a silent
fallback to the original file.
