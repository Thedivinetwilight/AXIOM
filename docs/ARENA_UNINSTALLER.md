# Arena Deep Uninstaller (`Arena-Uninstall.exe`)

A standalone Windows tool that removes an installed **AXIOM Arena** and finds
leftover or duplicate Arena/AXIOM files across the PC — including portable
copies that were never installed.

It is a **separate executable**: it does not need Arena to be installed,
running, or even present, and it bundles nothing from the application. That is
a hard design rule, not a convenience — the tool has to work precisely when
Arena is broken or half-removed.

---

## Where to get it

* **GitHub Actions** — the `Arena-Uninstall-windows-x64` artifact of the
  *Build executable* / *Arena uninstaller (Windows)* workflow runs.
* **GitHub Release** — attached to the release next to `Arena.exe` on tagged
  builds (`v*`).
* **Build it yourself** on Windows:

  ```bat
  pip install pyinstaller
  python build_uninstaller.py --test
  ```

  → `dist\Arena-Uninstall.exe`

The binaries are unsigned, so Windows SmartScreen asks once
(*More info → Run anyway*).

---

## What it does

### Two modes

| Mode | Scope |
| --- | --- |
| **Standard uninstall** (`--mode standard`) | The uninstall registration, the installer's own shortcuts, and the disposable parts of the install tree and per-user home. No drive walk. |
| **Deep clean** (`--mode deep`) | Everything above **plus** a scan of every accessible local fixed drive: duplicate executables, setup packages, extracted application folders, caches and leftovers. |

Deep clean requests elevation only when the scan actually hit locations it
could not read, and only offers to relaunch — never silently elevates.

### Detection — never by name alone

A file is classified as Arena-owned only when evidence proves it:

| Signal | Strength | Example |
| --- | --- | --- |
| Uninstall registration | strong | `AppId {B8E5C7A2-…}` or DisplayName **and** publisher `Thedivinetwilight` |
| PE version resource | strong | `ProductName = AXIOM Arena` **and** `CompanyName = Thedivinetwilight` |
| Known application location | medium | `%ProgramFiles%\AXIOM`, `%LOCALAPPDATA%\Arena`, the Kronos model cache |
| Application marker | medium | `app/__init__.py` declaring `__title__ = "arena"`, `instance.lock`, `paper-database-override.json` |
| File name | weak | `Arena.exe`, `AXIOM-Setup-3.2.4.exe`, `unins000.*` |

Weak signals never stand alone. "Arena Games", `AXIOM-notes.txt`,
`arena_tournament_data.csv`, another vendor's `unins000.exe`, or another
vendor's "Arena Tool" are listed as **ambiguous**, are never pre-selected and
are never removed automatically.

### The reviewable inventory

Before anything happens, every match is printed with **path, size, category
and the reasons it matched**:

```
   #  Sel  Category                       Size  Path
   4   x   leftover                     1.7 KB  C:\Program Files\AXIOM\Arena.exe
                                                  reason: tree content: Arena.exe (disposable)
   6       logs                           19 B  C:\Program Files\AXIOM\logs
                                                  reason: tree content: logs
  24   x   setup-installer              95.2 MB  C:\Users\me\Downloads\AXIOM-Setup-3.2.4.exe
                                                  reason: metadata: ProductName='AXIOM Arena', CompanyName='Thedivinetwilight'
                                                  reason: filename: file name matches Arena setup package
```

Interactive commands: `s N`/`d N` (select/deselect, ranges allowed), `sa`/`sn`
(all/none), `cat <category> on|off`, `protect on|off`, `l` (list), `p`
(proceed), `q` (cancel — nothing is changed).

Non-interactive equivalents: `--select-all`, `--select-category CAT`,
`--deselect-category CAT`, `--plan FILE`, `--apply FILE`, `--scan-only`,
`--json FILE`.

### Preserved by default

These categories are **not** deleted unless the operator enables the category
explicitly *and* selects the items:

| Category | Flag that unlocks it |
| --- | --- |
| trading history (`trading_bot.db`, `data/history`, `backups`) | `--include-trading-history` |
| logs | `--include-logs` |
| configuration (`.env`, `paper-database-override.json`, config files) | `--include-configuration` |
| credentials (`*token*`, `*credential*`, key stores) | `--include-credentials` |
| source repositories (Arena/AXIOM git checkouts) | `--include-source-repositories` |
| development folders (`.venv`, `node_modules`, caches used for development) | `--include-development` |
| research (`research/`, `results/`, notebooks) | `--include-research` |

A folder that contains any of them is **not removable as a unit** — the
disposable items inside it are listed separately, and the folder itself is
kept until every preserved item inside it has been dealt with.

### Confirmation, quarantine and the log

* Nothing is deleted without the exact phrase: type `DELETE` at the prompt, or
  pass `--confirm-delete DELETE` for unattended runs.
* The default action is **quarantine**, not deletion: items are moved to
  `%LOCALAPPDATA%\Arena-Uninstall\quarantine\<UTC timestamp>\` with a
  `manifest.json` (original path, size, category, reasons, SHA-256).
  Restore with:

  ```bat
  Arena-Uninstall.exe --restore "%LOCALAPPDATA%\Arena-Uninstall\quarantine\2026-10-20T12-00-00Z"
  ```

* `--permanent` deletes outright and is reported as **PERMANENTLY DELETED** in
  the output and the log. Failures (locked files, access denied) are reported
  item by item; nothing is retried destructively.
* A cleanup log is always written to `%LOCALAPPDATA%\Arena-Uninstall\logs\`.
  It records paths, sizes, categories, reasons and outcomes — never file
  contents, and every line is passed through a credential redactor.

### Its own files

The running `Arena-Uninstall.exe` and its PyInstaller extraction directory are
never targets: they are marked unselectable in the inventory, and if a copy
inside a tree is somehow selected, it is renamed and scheduled for deletion at
the next reboot (`MoveFileEx … DELAY_UNTIL_REBOOT`) rather than attacked while
running. Copies of the tool found elsewhere are recognised by their version
resource and are never pre-selected.

---

## Least privilege

* Runs as the invoking user. Per-user installs (`%LOCALAPPDATA%\Programs\…`)
  need no elevation at all.
* Elevation is offered only after a scan reports inaccessible locations, and
  only relaunches this same tool with the same arguments.
* It never disables security software, changes unrelated system settings,
  removes services, or touches scheduled tasks it did not create.
* Only the uninstall registration belonging to Arena (AppId / name+publisher)
  is ever removed from the registry.

---

## Command reference

```
Arena-Uninstall.exe [--mode standard|deep] [--scan-path DIR ...] [--no-drives]
                    [--scan-only] [--json FILE] [--plan FILE] [--apply FILE]
                    [--select-all] [--select-category CAT] [--deselect-category CAT]
                    [--include-trading-history] [--include-logs] [--include-configuration]
                    [--include-credentials] [--include-source-repositories]
                    [--include-development] [--include-research]
                    [--quarantine-dir DIR] [--permanent] [--dry-run]
                    [--confirm-delete PHRASE] [--report FILE] [--log-file FILE]
                    [--home DIR] [--restore DIR] [--no-elevation] [--elevate]
                    [--interactive | --no-interactive] [--list-categories] [--version]
```

Exit codes: `0` success, `2` usage error, `3` cancelled / no confirmation,
`4` completed with failures, `5` nothing could be scanned.

Typical sessions:

```bat
REM Look first — changes nothing
Arena-Uninstall.exe --mode deep --scan-only --json inventory.json

REM Review the plan, then apply exactly what it says
Arena-Uninstall.exe --apply inventory.json --confirm-delete DELETE

REM Unattended standard uninstall (data preserved)
Arena-Uninstall.exe --mode standard --no-interactive --confirm-delete DELETE
```

---

## How it is tested

* `tests/test_arena_uninstaller_detection.py` — signal combination, false
  positives, PE metadata, tree-content roles, duplicate discovery.
* `tests/test_arena_uninstaller_safety.py` — symlink/junction handling,
  permission failures, locked files, protected-data preservation, cancellation,
  confirmation, self-file handling, log redaction, quarantine restore.
* `tests/test_arena_uninstaller_cli.py` — the complete flow through the CLI,
  including plan/apply and the standard-library-only guarantee.
* `tests/test_arena_uninstaller_exe.py` — runs
  `packaging/uninstaller_e2e.py` against the **actual built executable**:
  scan, cancel, wrong confirmation, confirmed quarantine, preservation,
  restore, locked file, plan/apply, and the tool's own copy.

The Windows workflow builds `Arena-Uninstall.exe`, runs that e2e against it,
and only then uploads the artifact. A red run publishes nothing.
