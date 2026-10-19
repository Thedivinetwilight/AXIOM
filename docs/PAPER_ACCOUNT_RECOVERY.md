# Paper account recovery

*Arena refuses to start when the saved paper account cannot be restored under
the current configuration. This page explains what that means, what Arena
does about it, and exactly how to get back to work without losing anything.*

---

## 1. The message

```
Saved paper account is incompatible with the current configuration.
Differing fields: timeframe. Database: C:\Users\<you>\AppData\Local\Arena\trading_bot.db.
The saved paper account was not restored or changed.
```

Read it like this:

| Part | Meaning |
|---|---|
| `Differing fields: timeframe` | The **names** of the settings that no longer match the saved account. Never the values — the message is safe to paste into a support ticket. |
| `Database: <path>` | The exact file the account lives in. One file, one spelling — see §4. |
| `not restored or changed` | The account was **not** touched. Nothing was reset, migrated, deleted or overwritten. |

The launcher exits with **code 3** in this case. That is deliberate: it is a
refusal, not a crash.

## 2. Why the refusal exists

A saved paper account is not just a pile of numbers. It was created under
specific **execution assumptions**:

* the **timeframe** the candles are aggregated into (`1D`, `1m`, …),
* the **currency**,
* the **cost model** (commission, slippage, spread, position size, stops),
* the **calendar** the sessions are measured against.

Restoring the same fills under different assumptions would re-price the
account behind your back: the equity on screen would no longer be the equity
that was actually traded. So Arena refuses instead of guessing.

A refusal is only acceptable if it is *actionable*, which is what the rest of
this page is about.

## 3. What Arena does — and does not do

**It does:**

* run the check **read-only**, before the database is opened, so an
  incompatible account is never migrated, written or "repaired";
* tell you **which** fields differ, by name only;
* name the **exact file** the account lives in;
* keep the account, its orders, its fills and its research data **untouched**;
* offer a **documented recovery** that continues on a separate, empty paper
  account while the original stays exactly where it is.

**It does not:**

* create a new account behind your back;
* reset, migrate, delete, truncate or rewrite the saved account;
* bypass or weaken the compatibility check;
* change the execution mode. Recovery is `MODE=PAPER`, `LIVE_TRADING=false`,
  `BROKER=LOCAL_PAPER`, and it places no orders.

## 4. The database path is normalized

One installation means **one** database file. The path is resolved in exactly
one place (`app/runtime.py`), so the compatibility check, the database layer,
the diagnostics and the recovery command can never disagree about which file
is in play.

* In a **packaged install** the database lives in the per-user Arena home
  (`%LOCALAPPDATA%\Arena\trading_bot.db` by default).
* A **relative** `DATABASE_URL` in a packaged install is resolved against the
  Arena home, **not** against the process working directory. (It used to be
  handed to SQLite verbatim, which resolved it against the working directory
  — on Windows often `C:\Windows\System32` — so the same install could open
  a different account on every launch.)
* An **absolute** `DATABASE_URL` is always honoured exactly as written.

To see the resolved path, and where the setting came from:

```
Arena.exe --diagnostics
```

The output contains paths and field names only — never a secret, never a
configuration value.

## 5. Recovering

### 5.1 The recommended way: a separate, empty paper account

This keeps the original database exactly as it is and continues on a fresh
paper account. The choice is **persistent**: every later launch uses the new
database, so the installation does not fall back to the incompatible one.

From a console:

```
Arena.exe --recover-paper-account --confirm-new-empty-paper-account
```

From a source checkout:

```
python arena.py --recover-paper-account --confirm-new-empty-paper-account
```

The command:

1. **proves** the mismatch (read-only; it refuses if the account is
   compatible, so a healthy install is never "recovered");
2. creates a **new, empty** database under
   `%LOCALAPPDATA%\Arena\paper-recovery\`;
3. records the choice in `%LOCALAPPDATA%\Arena\paper-database-override.json`;
4. leaves the original database, its orders, its fills and its research data
   untouched.

The confirmation flag is mandatory. Recovery is a deliberate act, not
something a stray keystroke can trigger.

### 5.2 Undoing the recovery

To go back to the original account — for example after fixing the
configuration so it matches again:

```
Arena.exe --clear-paper-database-recovery
```

This removes **only** the selection file. Neither database is deleted. The
original account is then used again, and it starts normally once the
configuration matches it.

### 5.3 Fixing the configuration instead

If the account is fine and only the configuration drifted, change the
setting back to what the account was created with. The refusal tells you
which field moved; the saved value is visible in the account itself, and
`--diagnostics` shows the configured one.

The setup screen (`/setup`) and the wizard both write `DEFAULT_TIMEFRAME`.
When such a change is **refused** by the saved account, the configuration
file is **rolled back** — the installation is never left in a state where
every later launch fails.

## 6. The paper writer lock

Only one Arena may write to a database at a time. A second launch against the
same home opens the running session in the browser instead of starting a
conflicting server. If the running session is not answering, Arena says so
and refuses — it never kills a process and never deletes a lock file.

## 7. If the database itself is unreadable

```
Arena could not safely inspect the existing paper database <path> (DatabaseError).
No recovery database was selected and no paper account data was changed.
```

This means the file exists but could not be read as a database (truncated,
corrupt, or not a database at all). Arena refuses rather than replacing it.
Restore a verified backup (`python -m app backup`) or move the damaged file
aside by hand — never let the application overwrite it.

## 8. Where the state lives

| What | Where |
|---|---|
| Arena home (packaged) | `%LOCALAPPDATA%\Arena` |
| Database | `<home>\trading_bot.db` |
| Configuration | `<home>\.env` |
| Recovery databases | `<home>\paper-recovery\` |
| Recovery selection | `<home>\paper-database-override.json` |
| Logs | `<home>\logs\arena.log` |

`arena.log` records the resolved paths and the refusal reason of every
launch, which is the first thing to attach to a support request.

## 9. What the automated tests pin

`tests/test_paper_database_recovery.py` and `packaging/frozen_recovery_e2e.py`
cover, against the real code and the real executable:

* a changed timeframe is reported as `timeframe` and nothing else;
* no account or configuration **value** ever appears in the message;
* the check is read-only and happens before the database is opened;
* a compatible account still starts; an incompatible one stays protected;
* recovery creates a separate empty account and preserves the original
  database byte-for-byte;
* the selection survives a later launch and can be cleared without deleting
  a database;
* a refused configuration change is rolled back.

## 10. How the installed Windows build is verified

The unit tests cannot prove what a double-clicked `Arena.exe` does, so the
Windows build and installer workflow exercises the real thing on every
change to the application, the packaging or this document:

1. the full source suite runs on a Windows runner;
2. `Arena.exe` is built, and that executable is driven through this whole
   incident: it must refuse with exit code 3, name the differing field and
   the database, keep the saved account, recover into a separate empty
   account and start again;
3. the Inno Setup package is built, **installed silently**, and the
   **installed** executable is driven through the same refusal and
   recovery;
4. the package is uninstalled again, and the original database, the
   recovery database and the recovery selection must all still be there.

Each of those steps is a hard gate. If any of them fails, nothing is
published: the Windows artifacts are uploaded only when every gate passed.
