# AXIOM Chart Terminal — Windows x64

Install, launch, configure, upgrade and remove AXIOM's paper-trading chart
terminal.

> **Paper only.** AXIOM cannot place a real order. `MODE=PAPER`,
> `LIVE_TRADING=false` and `BROKER=LOCAL_PAPER` are enforced when settings load,
> re-checked by the launcher before the server starts, and every fill is made by
> an in-process simulator. The package contains no live broker client.
>
> **No profitability claim.** The bundled `DEMO`/`ALT` CSVs are sample data and
> the chart labels them *"DEMO REPLAY SAMPLE DATA · NOT MARKET DATA"*. No
> strategy in this build has out-of-sample, after-cost evidence.

The exact source commit, build environment, commands and test results are in
`BUILD_METADATA.json`, installed next to the program and attached to the release.

---

## 1. Requirements

- Windows 10 or Windows 11, **x64**.
- No administrator rights (per-user install), no separate Python,
  no Visual C++ redistributable step.
- A browser (Edge, Chrome or Firefox) for the chart.
- Internet only if you enable a live market **data** feed. The default `replay`
  mode is fully offline.

## 2. Install

1. Download the installer and `SHA256SUMS.txt`.
2. Verify the download:

   ```powershell
   Get-FileHash .\AXIOM-Chart-*-windows-x64-setup.exe -Algorithm SHA256
   ```

   Compare with the matching line in `SHA256SUMS.txt` or the release notes.
3. Run the installer. If SmartScreen says *"Windows protected your PC"*, choose
   **More info → Run anyway**: the binaries are not code-signed.
4. The wizard installs to `%LOCALAPPDATA%\Programs\AXIOM-Chart`, creates a Start
   Menu entry and (optionally) a desktop shortcut, and offers to start AXIOM.

**No installer?** `AXIOM-Chart.exe` is the same application as a single portable
file: run it from anywhere (including a USB stick). It writes nothing next to
itself.

## 3. First launch

Start **AXIOM Chart Terminal**. A console window opens — that window *is* the
application, and closing it stops AXIOM — and your browser opens at
`http://127.0.0.1:8000/chart`.

```
  ___  _  _ ___  __  __
 / _ \| || |_ _| \ \/ /   AXIOM chart terminal
| (_) | __ || |   >  <    chart + US paper data
 \___/|_||_|___| /_/\_\   PAPER ONLY - every fill is simulated

  config   C:\Users\<you>\AppData\Local\AXIOM\axiom.env
  data     C:\Users\<you>\AppData\Local\AXIOM
  feed     replay
  token    <your local API token>
           paste this when the chart asks; it stays on this machine

  chart    http://127.0.0.1:8000/chart
  stop     Ctrl+C in this window
```

On first run AXIOM creates, outside the program folder:

```
%LOCALAPPDATA%\AXIOM\
  axiom.env          your configuration (contains the local API token)
  axiom-paper.db     paper account: orders, fills, positions, P&L
  logs\              application log
```

The chart asks for the token the first time — copy the `token` line from the
console and paste it. It is remembered only for that browser tab. It protects
the local API from other programs on your machine; it is **not** a broker
credential.

Stop with **Ctrl+C** in the console (clean database shutdown).

## 4. Using the terminal

- **Symbol / timeframe buttons** in the header; `DEMO` and `ALT` are the bundled
  sample datasets.
- **SESSION** starts the paper engine from your configured `DATA_SOURCE`;
  starting a session is what makes bars flow. Press it again (or **STOP** in the
  dashboard) to stop.
- **Scroll** = zoom, **drag** = pan, **double-click** = reset, `F` = follow the
  newest bar, `R` = reset.
- **Ticket** (right panel): side, quantity or notional, `MARKET` or `LIMIT`.
  Click the chart to set a limit price. **PLACE** sends the order to the local
  simulator only. **FLATTEN** exits the position.
- **The feed badge is the one to trust**: `DEMO REPLAY SAMPLE DATA · NOT MARKET
  DATA` for sample CSVs, `ALPACA US MARKET DATA · LOCAL SIMULATED FILLS` when
  real market data is feeding the paper engine. `market_data: false` in the API
  payload means it is not market data.
- The **session badge** in US mode reads `OPEN` / `PRE_OPEN` / `CLOSED` from the
  New York calendar, not from your PC clock.

## 5. Configuration

Edit `%LOCALAPPDATA%\AXIOM\axiom.env` in Notepad and restart AXIOM. The file is
outside the program folder, so **upgrading never touches it**.

### 5.1 Safety switches — change these and the app refuses to start

```dotenv
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER
```

### 5.2 Everyday settings

| Key | Default | Meaning |
|---|---|---|
| `INITIAL_CAPITAL` | `100000` | starting cash (new database only) |
| `CURRENCY` | `INR` | account currency label |
| `DATA_SOURCE` | `replay` | `replay`, or `alpaca`/`upstox` for market data |
| `DEFAULT_SYMBOL` | `DEMO` | instrument shown first |
| `DEFAULT_TIMEFRAME` | `1D` | must match the feed's native timeframe |
| `API_TOKEN` | generated | local API token |
| `COMMISSION` / `SLIPPAGE` / `SPREAD` | `0.0005 / 0.0005 / 0.0002` | simulated costs |
| `STOP_LOSS` / `TAKE_PROFIT` | `0.05 / 0.10` | protective exits (fractions) or `none` |
| `POSITION_SIZE` | `0.25` | fraction of equity per strategy entry |
| `MAX_POSITION_VALUE`, `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS`, `MAX_TRADES_PER_DAY` | `0` (off) | optional hard caps |

Port: set the environment variable `AXIOM_PORT` before starting. Default 8000;
if busy, the next free port in 8000–8019 is used and printed.

## 6. US equities paper mode (read-only Alpaca market data)

Market **data** only — the package has no Alpaca order client.

1. Get a market-data key from your own Alpaca account. The free `iex` feed is
   the IEX venue only, not a consolidated tape; `sip` needs a paid entitlement.
2. Edit `axiom.env` and uncomment/fill **every** line:

   ```dotenv
   MARKET=us
   CURRENCY=USD
   MARKET_CALENDAR=us-equities
   DATA_SOURCE=alpaca
   DEFAULT_SYMBOL=AAPL
   DEFAULT_TIMEFRAME=1m
   ALPACA_API_KEY=...your key...
   ALPACA_API_SECRET=...your secret...
   ALPACA_DATA_FEED=iex
   ALPACA_SYMBOLS=AAPL,MSFT
   PAPER_MAX_POSITION_VALUE=1000
   PAPER_MAX_OPEN_POSITIONS=2
   PAPER_MAX_DAILY_LOSS=100
   PAPER_MAX_DRAWDOWN=0.05
   PAPER_MAX_TRADES_PER_DAY=4
   DATABASE_URL=sqlite:///C:/Users/<you>/AppData/Local/AXIOM/axiom-paper-us.db
   ```

   All five `PAPER_MAX_*` limits are required in this mode and have no defaults.
   Use a new `DATABASE_URL` whenever you change them or switch market.
3. Restart AXIOM, press **SESSION**, and confirm the feed badge says
   `ALPACA US MARKET DATA · LOCAL SIMULATED FILLS` and the session badge says
   `OPEN` during 09:30–16:00 New York (13:00 on early-close days).
4. Bars outside the New York session are dropped before they reach the engine
   and never appear on the chart. A quiet chart outside session hours is correct.

Keep `axiom.env` private — it can contain your market-data key. Never paste a key
into an issue, chat or screenshot.

## 7. Upgrading

Run the new installer. It replaces the program files in the same folder and
reuses the same Start Menu entry (stable application ID).

- **Your paper account, configuration and logs are kept** — they live in
  `%LOCALAPPDATA%\AXIOM`, which the installer never writes to.
- Your settings file is not rewritten, so an upgrade cannot silently change your
  risk caps.
- A paper database created with different account settings (or different risk
  limits) is refused rather than re-interpreted; AXIOM then asks for a new
  `DATABASE_URL`. Keep the old `.db` as an archive.
- **Rollback:** uninstall, then install the older version. Your data folder is
  untouched either way.

## 8. Uninstall

*Settings → Apps → Installed apps → AXIOM Chart Terminal → Uninstall*, or the
Start Menu uninstall entry.

Program files and shortcuts are removed, then the uninstaller asks:

> Your AXIOM paper account, settings and logs are kept in
> `…\AppData\Local\AXIOM`. Do you also want to DELETE that folder?

Choose **No** (default) to keep your paper history. A silent uninstall
(`/VERYSILENT`) always keeps it. To remove everything, delete
`%LOCALAPPDATA%\AXIOM` afterwards.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Console flashes and closes | Start it from PowerShell to read the message: `& "$env:LOCALAPPDATA\Programs\AXIOM-Chart\AXIOM-Chart.exe"` |
| `this build is paper-only and will not start with: MODE=…` | Your `axiom.env` asks for something other than paper. Set `MODE=PAPER`, `LIVE_TRADING=false`, `BROKER=LOCAL_PAPER`. |
| `DATA_SOURCE=alpaca requires ALPACA_API_KEY and ALPACA_API_SECRET` | Fill both keys, or set `DATA_SOURCE=replay`. |
| `US paper mode requires PAPER_MAX_…` | All five `PAPER_MAX_*` values must be present, positive, and drawdown between 0 and 1. |
| `Saved paper risk limits differ; use a new database` | You changed caps on an existing account. Use a new `DATABASE_URL` or restore the old caps. |
| Chart returns **503** | `API_TOKEN` is empty. Delete `axiom.env` to regenerate one (your database is kept). |
| Chart asks for the token again | The browser tab was closed. Paste the token printed in the console. |
| `no bars for this symbol yet` | No session is running for that symbol. Press **SESSION**, or pick `DEMO`/`ALT`. |
| US chart empty during the day | Check the header says `OPEN`; outside 09:30–16:00 New York nothing is expected. Confirm `/paper/status` shows `provider: alpaca` and a fresh `last_candle_time`. |
| `no free local port` | Something occupies 8000–8019. Set `AXIOM_PORT=8050` before launching. |
| Port 8000 shows a different app | An older AXIOM instance is running. Close it, or use `AXIOM_PORT`. |
| `database is locked` | Two instances share one data folder. Close the extra one. |
| SmartScreen or antivirus blocks the exe | Unsigned build. Verify the SHA-256, then allow it. |
| Start over with a clean paper account | Close AXIOM, rename `%LOCALAPPDATA%\AXIOM` to `AXIOM-old`, start again. |

**Logs:** `%LOCALAPPDATA%\AXIOM\logs\`. A log contains no credentials and is safe
to attach to a bug report — `axiom.env` is not.

## 10. Known limitations

1. **Unsigned binaries.** SmartScreen may warn; verify the SHA-256.
2. **No historical backfill in the chart.** It draws bars the running engine
   processed (max 1,500 per symbol, in memory); a restart redraws from the feed.
3. **Streaming is completed 1-minute bars only** in US mode. Other timeframes
   come from exact intraday aggregation; daily bars are never synthesised.
4. **Token re-entry per browser tab**, by design of the page.
5. **Offline holiday calendar.** Regular NYSE holidays and 13:00 early closes
   are computed locally; an exceptional exchange closure is not auto-discovered.
6. **Upstox sandbox routing is not host-locked in this source line.** The
   packaged launcher can only start the local paper broker and the API constructs
   nothing else, but the repository also contains an Upstox *sandbox* order client
   used by the CLI (`python -m app engine`) whose host check only warns. It is
   unreachable from the installed application.
7. **No profitability claim** for any strategy in this build.
8. **The monitoring and maintenance modules are POSIX-only in this source line.**
   `app/monitoring` imports the POSIX `resource` module, so on Windows three
   tests fail on import (`ModuleNotFoundError: No module named 'resource'`) and
   two `TestMaintenanceWiring` tests fail as a consequence. Two further tests
   assume POSIX behaviour (a `chmod`-based file-mode assertion in
   `tests/test_upstox_v062.py` and a CLI exit code in
   `tests/test_v2_api_cli.py`). All 7 are outside the chart-terminal path:
   the focused US-paper/chart suites pass, as do the 21 on-machine application
   checks and the 18-step installer acceptance test. They are listed, not
   hidden — see §12.

## 11. Where the paper-only guarantee is enforced

| Guarantee | Enforcement |
|---|---|
| only PAPER | `app/config/settings.py` raises for any other `MODE`/`LIVE_TRADING`; the launcher refuses before the server starts |
| only the local broker | `_require_local_paper()` gates `POST /paper/order`; the API constructs only `PaperBroker` |
| no live routing | the package has no live-broker client and no live order endpoint |
| visible paper mode | the chart header always shows `PAPER · SIMULATED ORDERS`; every payload carries `paper_only: true`, `live_trading: false` |
| honest data labelling | every chart payload names its feed; sample CSVs are labelled *NOT MARKET DATA* |

## 12. What was tested, and how to check this download yourself

The published build is compiled on a Windows x64 machine from one exact commit.
Its record travels with it:

| File | What it is |
|---|---|
| `BUILD_METADATA.json` | the source commit, its subject and date, the runner and toolchain versions, every test result and the SHA-256 of each artifact |
| `SHA256SUMS.txt` | the same hashes in `sha256sum -c` format |
| `SOURCE_COMMIT.txt` | the installed record of the commit, also copied into the install folder |
| `install-test-log.txt` | the 21 on-machine checks of the frozen executable (launch, `/health`, `/chart`, chart assets, token gate, paper order path, fail-closed cases) |
| `installer-test-log.txt` | the 18-step installer acceptance test: silent install, launch of the installed copy, `/chart` with the PAPER badge, bundled assets, 401 without a token, silent uninstall that keeps operator data |
| `pytest-focused.log`, `pytest-full.log` | the Windows test runs, complete |

Every release asset is listed in the release notes with its SHA-256. To check
what you downloaded:

```powershell
Get-FileHash .\AXIOM-Chart-*-setup.exe -Algorithm SHA256
# or, if you keep SHA256SUMS.txt beside the file:
Get-Content SHA256SUMS.txt | ForEach-Object {
    $hash, $name = $_ -split '\s+', 2
    $actual = (Get-FileHash ".\$name" -Algorithm SHA256).Hash.ToLower()
    "{0}  {1}" -f $(if ($actual -eq $hash) { 'OK  ' } else { 'FAIL' }), $name
}
```

The binaries are unsigned, so SmartScreen may warn: check the hash against the
release notes before choosing *More info → Run anyway*.
