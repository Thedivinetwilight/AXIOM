# AXIOM Operator — User Manual

**Current source version: 3.7.0a8**

**Audience:** the account owner operating AXIOM on Windows x64.

> **Version 3.7.0a8 Parallel Strategy Ensemble, Portfolio Risk Allocator & Live Trade Monitor:** The dark `/terminal` console (`01`–`12`) now adds Workspace **`11 Live Trade Monitor`** (strictly read-only real-time candlestick & 8-shape signal/order/fill/SL/TP/exit inspector with unmistakable `PAPER` vs `LIVE` separation and `"NOT TRADING"` inactive banners) and Workspace **`12 Ensemble & Allocation`** (`docs/ENSEMBLE_PROFITABILITY_EVIDENCE_AND_BENCHMARKS.md`, `docs/AI_MODEL_LAB_MANUAL.md`, `docs/OWNER_SETUP_GUIDE.md`) supporting up to `1,024+` bounded parallel strategy variants, OOS walk-forward calibration, optional advisory Kronos, and a single per-market Portfolio Risk Allocator. All research, calibration, and monitor surfaces are strictly PAPER/RESEARCH/READ-ONLY and never authorize or alter live execution.

AXIOM now has three mutually exclusive execution modes:

| Mode | Execution | What it means |
|---|---|---|
| **LOCAL PAPER** (`AXIOM_PAPER`) | AXIOM's local simulator | Independent paper state and clearly simulated fills. An optional, separate VANTAGE DEMO read-only feed can supply prices; it never routes a broker order. |
| **VANTAGE DEMO** | Vantage MT5 demo account | Broker-executed demo orders after explicit review, Arm and Activate. Demo is not AXIOM paper. |
| **VANTAGE LIVE** | Vantage MT5 live account | Real-money capability, disabled until the account owner completes every in-app gate and confirms each order. |

> **No profitability or live-readiness claim.** Tests, a successful connection, and demo fills do not establish profitability or readiness for real money. AXIOM does not promise returns, execution quality, or a particular product offering. Live credentials and live activation are solely the account owner's responsibility.

The Vantage integration uses the official `MetaTrader5` Python package through a local MetaTrader 5 terminal. It does not scrape the Vantage website or automate the terminal with screen coordinates. In a Vantage execution session, quotes, contract metadata, account state and execution come from the same terminal/account connection. In LOCAL PAPER, the optional MT5 feed is a different service with a read-only gateway: it can discover contracts and read ticks/closed bars, but it has no order, position, history, margin-precheck or execution methods. Connecting it never changes the execution mode.

For Windows download, installation and upgrade steps, read `packaging/windows/WINDOWS_INSTALL_MANUAL.md` or the copy installed beside the application. Historical Arena research and paper/sandbox material remains in `README.md`; it does not describe the Vantage order path.

Version 3.7.0a1 also includes a **separate Daily Upstox Autonomy** boundary at `/autonomy`. Its live-market local PAPER, default-disabled UPSTOX_LIVE real-money path, exact listed-equity/currency-derivative catalog, unattended daily-session rules, three-leg GTT protection, reconciliation, Kronos artifacts and paired-ablation research are documented in `docs/AUTONOMY.md` (also served at `/autonomy/manual`). It does not alter the Vantage rules above. No Upstox account-specific or external DEMO/LIVE verification is implied.

Optional Kronos setup is an explicit button on that page. Review the complete pinned sources, revisions, sizes, hashes and CPU runtime lock before confirming. Windows discovery includes `py -3.11-64` / `py -3.12-64` and the `py -0p` registered-interpreter catalog. If needed, use **Validate & use Python** with one full `python.exe` path (no arguments), or **Auto-detect Python** to clear that preference. Only the CPython 3.11/3.12 x64 executable itself with pip is accepted (not a launcher or command line); safe probe diagnostics omit local paths and raw stderr. Selection is stored in per-user Kronos data and does not alter trading state. Weights/runtime install only into per-user autonomy data storage, never the application directory. Cancellation removes partial setup files; failure remains `UNAVAILABLE` with no mock forecast. The separate offline self-test performs real inference on synthetic smoke bars only—it is not market/profitability evidence and does not create, Arm or Activate a trading session.

For that autonomy boundary, **Scan once is read-only in both PAPER and LIVE**; only the background worker executes orders for an explicitly ACTIVE daily session. Emergency stop blocks new entries before cleanup, attempts managed unfilled-entry cancellation without requiring ACTIVE, preserves open-position protection, and remains prominently unresolved when cancellation/reconciliation is uncertain—it never claims the account is clear. Restart/browser recovery exposes durable sessions, positions, intents, and orders as HALTED read-only views, never restores ARMED/ACTIVE, and requires fresh authorization/risk/reconciliation/approval. Synthetic research evidence cannot execute even in PAPER; operator-supplied provenance is not independent verification.

---

## 1. Safety model — read before connecting an account

### 1.1 Selection is not authorization

Selecting **VANTAGE DEMO** or **VANTAGE LIVE**, saving credentials, or passing a read-only connection test does **not** Arm or Activate trading. AXIOM starts disconnected and disarmed. Restart, reconnect, credential changes, risk changes, instrument-selection changes, ambiguous outcomes and other relevant changes invalidate prior approval.

`VANTAGE LIVE` on the mode badge means the live environment is selected. It does not, by itself, mean an order can be sent. Check **Connection**, **Lifecycle**, **Emergency stop**, and the current readiness result separately.

### 1.2 Separate gates

1. **Mode:** explicitly choose PAPER, DEMO or LIVE.
2. **Connection:** run the read-only MT5 test and verify the masked login, exact server and account type.
3. **Configuration:** select account-discovered instruments and save bounded risk limits.
4. **Readiness:** every server-side check must pass. Unknown or unavailable checks fail closed.
5. **Arm:** enter the exact environment-specific Arm phrase using a fresh readiness challenge.
6. **Activate:** enter a second, distinct phrase using a fresh activation challenge.
7. **Order review:** inspect the current contract, quote, margin/risk result and read-only MT5 precheck.
8. **Final confirmation:** enter the exact order phrase before the 30-second review expires. Submission repeats the checks.

Arming or activating never submits an order. Vantage strategy automation is unavailable and off in this version; activation enables reviewed manual order flow only.

### 1.3 Pause, Stop and Emergency stop

- **Pause** blocks new entries. It does not claim that working orders were cancelled or positions closed. Reactivation requires the displayed challenge and phrase.
- **Stop** stops the session and makes one cancel attempt per AXIOM-managed working order when connected, then reconciles. External orders are left untouched. Read the returned broker state.
- **Emergency stop** first sets a durable local halt, stops the session, and attempts managed-order cancellation when connected. The halt survives restart. Clearing it requires `CLEAR EMERGENCY STOP`; clearing still leaves the session disarmed.
- Neither Stop nor Emergency stop proves that the account is flat, cancelled, or safe. Verify orders, positions and deals from the broker. If MT5 is disconnected, AXIOM cannot cancel anything at the broker.

### 1.4 Ambiguous outcomes

A timeout, exception, unrecognized retcode, or failed post-action reconciliation can leave an outcome unknown. AXIOM records the intent before submission, makes only one submission attempt, and does not blindly retry. An unresolved intent blocks readiness.

Do not submit a replacement merely because the first result is unclear. Open the MT5 terminal, compare AXIOM's intent/comment and broker order/deal history, then use **Reconcile**. AXIOM adopts only a strict unique managed match. Multiple possible matches or missing evidence remain unresolved. Never delete the database or change an idempotency key to bypass this protection.

---

## 2. Requirements and installation

### 2.1 Windows and MT5

- Windows 10/11 **x64** for the packaged application.
- A modern browser. Keep the AXIOM console window open while using the app.
- A Vantage MetaTrader 5 desktop terminal obtained through Vantage's official instructions.
- The official MT5 terminal must be able to connect to the intended account/server. AXIOM's official Python package is bundled in the Windows build; the MT5 desktop terminal is a separate prerequisite.
- Terminal and account permissions must allow the intended operation. If the terminal requires an algorithmic-trading permission, configure it deliberately in MT5 and follow Vantage's rules.

The operator's **MT5 package available** capability only proves that the official Python module is present. It does not prove that the terminal exists, is running, can log in, is connected, or is permitted to trade.

### 2.2 Install or run portable

1. Download the Windows x64 `*-no-temp-install.zip` or `*-portable.zip` and the published `SHA256SUMS.txt` from the same release.
2. Verify the exact ZIP hash before extracting it. Release binaries are **unsigned unless the release metadata explicitly says otherwise**; SmartScreen/Smart App Control can warn or block them.
3. For installation, extract the entire installer ZIP, keep the setup EXE and matching `-0.bin`/`-1.bin` together, then run the EXE. The per-user installer does not require administrator rights and installs under `%LOCALAPPDATA%\Programs\AXIOM-Chart` for upgrade compatibility, while the product name is **AXIOM Operator**.
4. Alternatively, extract the entire portable ZIP and run `AXIOM-Chart.exe` inside `AXIOM-Operator` with `_internal` beside it. The onedir folder avoids onefile TEMP extraction and stores operator data in the per-user data directory, not beside the executable.
5. AXIOM opens `http://127.0.0.1:<port>/terminal`. The displayed local address is not a public hosted service.

For the reported Error 4551, these loaderless/onedir packages avoid the identified Inno setup-loader and onefile runtime TEMP paths. They do not make unsigned code trusted or bypass Windows policy. Keep Application Control enabled; if the device still blocks the package, trusted signing or administrator approval is required. Read `WINDOWS_APP_CONTROL.md`.

The release's `BUILD_METADATA.json` is the authoritative record of source commit, runner, commands, test results, skipped checks and signing status. A source archive is not a verified Windows binary.

### 2.3 Local API token

The local `API_TOKEN` protects AXIOM's loopback API. It is **not** a Vantage password, MT5 login, or market-data credential. The launcher does not print it.

When the browser asks for it:

1. Open `%LOCALAPPDATA%\AXIOM\axiom.env` in Notepad.
2. Copy only the value after `API_TOKEN=` into the browser's token field.
3. Keep the file private. The page remembers the token only for that browser tab.

Do not paste the token, `axiom.env`, broker passwords, or full account credentials into chat, screenshots, issues, logs, or CI.

---

## 3. Operator pages

The top mode badge and environment labels remain visible across the eight pages. A three-part boundary banner separately identifies the **execution session**, **market-data source**, and **order authorization** at all times. DEMO data connected while execution is LOCAL PAPER never changes that execution label. The layout adapts to narrow screens, keeps safety controls visible, supports keyboard focus/modal trapping, and honors reduced-motion and increased-contrast preferences.

| Page | Purpose |
|---|---|
| **Overview** | Current mode, connection/lifecycle, broker or paper account state, positions, exposure and explicit limitations. |
| **Instruments** | Account-specific discovery, product/contract details, search, editable tab-local watchlists, execution selection and tradability. Watch and execution-selection controls are separate. |
| **Charts** | Account-discovered MT5 bars/contracts for Vantage execution or the independent DEMO feed; replay remains explicitly labeled. Includes timeframe shortcuts, zoom/pan, crosshair/OHLC readout, EMA/SMA overlays, horizontal/trend drawings, manual-only MT5 paper control and a docked reviewed ticket. |
| **Orders / Positions** | Broker-truthful orders, deals, positions, P&L where available, durable intents, reviewed cancel/close/protection modification. |
| **Risk** | Required bounded limits, current readiness checks, Arm/Activate/Pause/Stop and durable emergency controls. |
| **Connections** | Separate demo/live credential metadata, execution read-only test, disconnect, replace/delete and non-secret capabilities. In LOCAL PAPER, only the VANTAGE DEMO profile can be edited for the independent quote feed. |
| **Activity / Audit** | Redacted local operational events and unresolved outcomes. No external alert delivery is claimed. |
| **Settings / Help** | Local token handling, refresh/redacted diagnostics, separate latency/render measurements, listed-share/ETF provider scope, links to the legacy chart/manual, mode explanations and recovery guidance. |

Controls that cannot run in the current state are disabled or return an explanatory blocker. An empty disconnected broker view is **not** a statement that the account has no positions or orders; look for `broker state unavailable` / no reconciliation timestamp.

---

## 4. Connections and credentials

### 4.1 Add a demo credential first

Use a demo account explicitly supplied by Vantage if you want to rehearse the workflow. Do not use a live login to test demo mode.

For **fresh prices with local paper fills**, do not follow the execution-mode switch below: keep **LOCAL PAPER**, save the DEMO-only profile, and use **Connect demo market data — read-only** as described in section 4.5. The execution **Test connection** button remains unavailable in paper mode.

For a **Vantage demo execution** session:

1. Choose **VANTAGE DEMO**.
2. Open **Connections**.
3. Enter the numeric MT5 login, trading password, exact demo server shown by Vantage, an optional local label, and the MT5 terminal path if auto-discovery is insufficient.
4. Choose **Save credentials**. If a profile already exists, use the explicit **Replace** flow instead.
5. Choose **Test connection (read only)**.

The read-only test initializes/logs into MT5, verifies identity, discovers account instruments, and reads/reconciles account state. It does not place, modify or cancel an order.

### 4.2 Live server selection

For the requested environment, the exact server choice is:

```text
VantageMarkets-Live 13
```

Enter/select that exact text only if it is the server Vantage assigned to **your** live account. Select **VANTAGE LIVE** separately. The server string alone never selects or activates live trading. A real-account identity mismatch fails readiness.

### 4.3 Protected storage and metadata

On Windows, broker secrets are encrypted with **Windows DPAPI for the current Windows user**. AXIOM stores separate demo and live profiles. Public views contain only masked login metadata, server, terminal path and label; password and full login are not returned by the API.

DPAPI protection is tied to the Windows user/machine context. After moving to another user or machine, re-enter credentials through the app. There is no plaintext credential export. Deleting a profile removes its protected secret and metadata; replacing or deleting the current environment disconnects and disarms it.

Keep these credentials separate from:

- `API_TOKEN` in `axiom.env` — local AXIOM API only;
- Alpaca/Upstox or other market-data credentials — legacy paper/data features only;
- Vantage demo and live profiles — never interchangeable.

### 4.4 Read-only test results

A passing test confirms only the observed connection, identity, discovery and read/reconcile operations at that time. It does not confirm future connectivity, order acceptance, margin availability at a later time, profitability, or live readiness. Disconnecting invalidates Arm/Activate.

### 4.5 Independent DEMO market data in LOCAL PAPER

The optional `VANTAGE DEMO MT5` feed is for market data only. It uses the saved **VANTAGE_DEMO** DPAPI profile and refuses a non-DEMO MT5 account. It never reads the VANTAGE LIVE profile and never selects or activates that execution mode. Saving credentials, connecting, disconnecting and loading a chart create zero orders.

Its isolated gateway exposes only terminal initialization/login, terminal/account identity, account-symbol discovery/metadata/selection, current ticks, closed rates, constants needed to interpret them, and shutdown. It does not expose order placement/check/modify/cancel/close, positions, orders, history or margin calculation. The UI/API cannot reach the execution gateway through this service.

Deleting the DEMO credential, selecting a Vantage execution environment, or explicitly disconnecting the feed closes that independent MT5 connection. LOCAL PAPER account state is preserved. A status of `CONNECTED` alone is not enough to start MT5 paper: a supported exact symbol must have a fresh, valid, advancing tick.

---

## 5. Instruments and product metadata

AXIOM calls MT5 `symbols_get` for the connected account. It does not hard-code a Vantage universe and does not infer availability from Vantage marketing pages. Entity, jurisdiction, account type and server can change the offering.

1. After a successful read-only test, open **Instruments**.
2. Search/filter the returned symbols and inspect each contract.
3. Select only the instruments you intend to use (up to 50 distinct symbols).
4. Save the **execution selection**. Changing it disarms the session and requires readiness/Arm/Activate again.

The star column is a separate editable watchlist. **Watch visible**, **Clear watchlist**, and **Watchlist only** affect only this browser tab's watchlist, capped at 200 exact discovered symbols and scoped by execution mode plus the existing opaque account fingerprint. Watching, unwatching or filtering sends no selection, connection, feed, Arm/Activate or order request and does not change the execution checkboxes. Only **Save execution selection** calls the selection API.

The detail view exposes available fields such as broker path/description, product classification, CFD flag, currencies, volume min/max/step/limit, contract size, tick size/value, digits/point, trade mode, sessions and filling/execution flags where MT5 supplies them.

AXIOM distinguishes forex, indices, commodities, share CFDs, ETFs/ETF CFDs and other products. **A share CFD or ETF CFD does not represent owned underlying shares or ETF units.** Classification assists display; the broker's actual contract metadata governs the order checks.

An instrument is rejected for order flow when it is unavailable, not selected, disabled/close-only for the requested action, stale, or has incomplete/inconsistent metadata needed for safe sizing and validation. A displayed symbol is not a promise that an order will be accepted.

---

## 6. Quotes and charts

Vantage execution quotes use the same MT5 connection as that execution session. Independent paper quotes use only the separate DEMO read-only service. Quote validation includes positive ordered prices, finite values, tick time, receive time, maximum age and spread. A chart's historical bars do not prove that the current quote is fresh.

To view a Vantage execution chart, select the instrument first, then choose a supported MT5 timeframe on **Charts**. Its label identifies DEMO or LIVE and states that it is the same MT5 account source as execution. DEMO market data can be real broker data while execution fills remain demo; do not confuse that with LOCAL PAPER simulation.

For independent paper charts, connect the DEMO feed and choose an exact account-discovered symbol on **Charts**. The label says **FRESH READ-ONLY MARKET DATA · LOCAL PAPER FILLS** only when a current tick is fresh and advancing. Bars are validated, newly closed MT5 bars; chart history is not replayed into the paper strategy. Broker tick volume is labeled as such. The contract panel preserves the exact symbol, product/CFD classification, currencies, contract size, tick/point metadata and MT5 lot metadata for inspection, while the paper ticket explicitly uses **units, not MT5 lots**.

The chart displays bid, ask, price spread, source timestamp, local receive timestamp and freshness/age where the provider supplies a tick. Use the timeframe selector or shortcuts; mouse/touch drag pans, the wheel and +/− controls zoom, and **Reset view** returns to the latest bounded window. With the canvas focused, Left/Right pan, +/− zoom and Home resets. The crosshair shows time and OHLC values. EMA 20, SMA 50 or both are calculated from loaded closes. Horizontal and two-click trend drawings are kept per chart identity in memory, capped at 50; **Clear drawings** affects only the current chart. Escape cancels a pending trend point. These analysis gestures never change the server series, select an execution instrument, connect a feed, or create an order.

A disconnected, stale, non-advancing, invalid, incomplete or refused feed is labeled with its reason. MT5 chart failures return an error and clear the chart; AXIOM never silently substitutes replay, cache, fixture or synthetic bars. Feed loss blocks new BUY entries but preserves paper cash, positions, orders and fills; a reviewed SELL exit of an existing long position remains available. Reconnect and verify the exact symbol/fresh quote before new entries.

The legacy `/chart` page supports replay/US-paper workflows. Its sample CSVs are labeled sample data, and Alpaca/US-paper fills remain locally simulated. It is not a Vantage order ticket.

TradingView integration and relayed webhooks are deferred. No TradingView alert is treated as an order in this version.

---

## 7. Risk limits

Open **Risk**, enter every limit, and save. Values are per selected environment. Saving risk changes disarms the session.

| Limit | Enforcement purpose |
|---|---|
| Maximum order volume | Absolute cap before contract min/max/step normalization checks. |
| Maximum symbol volume | Caps aggregated exposure for the symbol, including relevant open/working exposure. |
| Maximum order margin | Caps estimated margin for one entry using MT5 margin calculation. |
| Maximum total margin | Caps current plus estimated account exposure. |
| Maximum open positions | Bounds the broker-reported open-position count. |
| Maximum daily loss | Uses a persisted day-start equity baseline and broker P&L/cost data. |
| Maximum drawdown % | Uses the observed persisted peak-equity baseline. |
| Maximum trades per day | Bounds AXIOM-recorded submissions for the environment/day. |
| Maximum margin usage % | Bounds margin relative to account equity. |
| Maximum spread bps | Rejects an excessively wide current quote. |
| Maximum slippage points | Sets the MT5 request deviation bound; it is not a guaranteed fill bound. |
| Maximum quote age seconds | Rejects stale or invalid quote timestamps. |

Limits are necessary guards, not a complete risk-management guarantee. Gaps, rejected protective orders, disconnections, broker changes, contract events and execution differences can produce larger losses. Balance operations can make a day baseline untrustworthy; AXIOM then fails readiness rather than inventing one.

---

## 8. Readiness, Arm and Activate

On **Risk**, run **Readiness** after connection, selection and risk configuration. Checks include:

- explicit Vantage environment and verified terminal connection;
- exact server, masked account identity and DEMO/REAL account type;
- account and terminal trading permissions;
- selected instruments, fresh valid quotes, clock sanity, market/symbol availability and complete contracts;
- valid bounded risk limits, funds/margin, daily baseline and recent reconciliation;
- no unresolved intents and no unexplained external positions/orders;
- emergency stop clear, valid single-instance writer lock, writable audit store and local alerting availability;
- automation off/unavailable.

Any failed, unknown or unavailable check blocks Arm. A challenge expires after 60 seconds. Use the exact phrases displayed for the selected environment:

```text
ARM VANTAGE DEMO
ACTIVATE VANTAGE DEMO

ARM VANTAGE LIVE
ACTIVATE VANTAGE LIVE
```

Arm and Activate are separate. Activation submits zero orders and does not start a strategy. Relevant changes or failures disarm the session; never treat an old screenshot or previous readiness result as current approval.

---

## 9. Reviewed order flow

### 9.1 New entry

1. Complete activation for the intended environment.
2. On **Charts**, use the docked ticket to choose a selected symbol, BUY/SELL, MARKET/LIMIT, volume and optional SL/TP. Use the displayed contract units—MT5 volume is lots, not a share count; MT5-priced LOCAL PAPER explicitly uses units and does not perform lot/contract/FX conversion.
3. Generate a new idempotency key for a genuinely new intention. Reusing a key returns/blocks the existing intent rather than sending another order.
4. Choose **Review**. AXIOM refreshes/reconciles broker state, contract and quote; normalizes volume/price; checks session, emergency stop, identity, exposure, margin, risk and MT5's read-only `order_check` precheck.
5. Inspect the 30-second review: environment, masked account/server, contract, quote age/spread, estimated price/margin, all blockers and the exact confirmation phrase.
6. Only if every check passes, type:

   ```text
   SUBMIT DEMO ORDER
   ```

   or, for an explicitly authorized live session:

   ```text
   SUBMIT LIVE ORDER
   ```

7. Final submission repeats all checks, persists the intent, and makes one `order_send` attempt. Read the resulting intent and broker reconciliation—not just the button animation.

The docked ticket shows product/CFD context, quantity units, indicative value/currency, bid/ask/spread, cost limitations, margin context and risk state. Indicative value is units × price for paper and lots × broker contract size × price for Vantage; it is not a guaranteed account-currency value or fill. AXIOM does not invent commission/fee values. MT5 margin and server risk appear from the actual read-only review, and editing the ticket clears that review context. Broker deals remain authoritative for filled price, commission, swap, fee and P&L.

A read-only precheck passing does not guarantee a fill. AXIOM does not report `FILLED`, `CANCELLED` or `MODIFIED` without supporting broker evidence. Demo fills are broker demo outcomes, not evidence of live execution or profitability.

### 9.2 Close a position

A close must reference a currently broker-reported position ticket, use the opposite MARKET side, and not exceed its confirmed volume. Review and type `CLOSE DEMO POSITION` or `CLOSE LIVE POSITION`. Actual filled volume, costs and P&L come from broker deals. If the position disappears or changes, reconcile and review again.

### 9.3 Cancel a working order

Only a reconciled AXIOM-managed working order can be cancelled through its intent. Type `CANCEL DEMO ORDER` or `CANCEL LIVE ORDER`. One cancel attempt is followed by reconciliation. If the outcome is not broker-confirmed, it remains unresolved and the session disarms.

### 9.4 Modify position SL/TP

Protection modification is a separate reviewed, position-ticket-bound action with its own idempotency key and `MODIFY DEMO POSITION` / `MODIFY LIVE POSITION` confirmation. AXIOM confirms `MODIFIED` only when the resulting broker position matches the requested SL/TP values. Pending-order modification is not exposed in this version.

### 9.5 What AXIOM does not manage

Externally created/manual MT5 orders and positions are reported where possible but are not assumed owned by AXIOM. They block Arm as unexplained exposure. Stop/Emergency stop leave external orders untouched. Manage them deliberately in MT5, then reconcile; AXIOM never claims to have flattened them.

---

## 10. Broker state, P&L and audit

Vantage account snapshots, working orders, order history, positions and deals are read from MT5. P&L includes available broker-reported components; the UI distinguishes realized history, unrealized position P&L and data availability. A disconnected view cannot provide current broker P&L.

**Activity / Audit** records redacted events such as mode selection, credential metadata changes, connection tests, readiness/lifecycle transitions, reviews, intents, broker results, reconciliation and emergency actions. Local replay additionally records run-scoped start/running, actionable strategy signals, simulated orders/fills and explicit stop reasons as described in section 13. Secrets and full logins are excluded. Local event persistence is not an external notification service.

Keep the console and local logs available for diagnosis, but inspect any file before sharing it. Never share `axiom.env`, DPAPI credential files, screenshots containing account details, or unreviewed support bundles.

---

## 11. Restart and recovery

1. **Ordinary restart:** expect disconnected/disarmed and automation off. Re-run the read-only test, reconcile, readiness, Arm and Activate. Saved credentials/selection/risk do not authorize trading.
2. **Reconnect:** prior activation is invalid. Confirm exact account/server and fresh broker state before proceeding.
3. **Unknown order result:** do not retry. Compare intent/comment, working orders, history and deals in MT5; run Reconcile. Multiple/no safe matches remain blockers.
4. **Unrecognized external exposure:** inspect it in MT5. AXIOM will not adopt or cancel it merely to make readiness pass.
5. **Emergency stop after restart:** the durable flag remains set. Verify broker state independently, then explicitly clear it if appropriate; full readiness and activation are still required.
6. **Stale review/challenge:** generate a fresh one. Never reuse an expired review after prices or positions change.
7. **Database/audit failure or lost writer lock:** readiness fails closed. Close duplicate AXIOM instances and restore exclusive access; do not bypass the check.
8. **Backup:** close AXIOM before copying the SQLite database and configuration. Treat DPAPI files as sensitive; they are not portable plaintext backups and may not decrypt for another user.

Do not “fix” a blocker by deleting journals, editing intents, disabling risk limits, or launching a second writer. Those actions destroy the evidence needed for safe reconciliation.

---

## 12. Troubleshooting

| Symptom | What to check |
|---|---|
| MT5 package unavailable | Use the current Windows x64 build; inspect `BUILD_METADATA.json`. Package presence is separate from terminal connectivity. |
| Terminal unavailable / initialize failed | Install/open Vantage MT5, verify the executable path, terminal architecture and Vantage instructions. No website fallback exists. |
| Authentication failed | Recheck numeric login, trading password and exact server. Use Replace deliberately; never send the password for support. |
| Server/account mismatch | The selected DEMO/LIVE mode or exact server does not match MT5. Disconnect and correct it; do not Arm. |
| Credential storage unavailable | Packaged Vantage credentials require Windows DPAPI. Source/fake tests using memory stores are not production secret storage. |
| No instruments | Complete the read-only test on the intended account. Availability is account-specific; an empty result is not filled from a preset universe. |
| Instrument untradable | Inspect trade mode and metadata issues. Incomplete products, disabled symbols, unselected symbols and invalid quotes are rejected. |
| Readiness blocked by external items | Review all MT5 positions/orders. AXIOM requires explained exposure before Arm. |
| Readiness blocked by unresolved intent | Reconcile against broker evidence. Do not submit a duplicate or delete the journal. |
| Readiness blocked by equity baseline | Balance operations or missing state can prevent a trustworthy daily baseline. Resolve with broker records; do not invent a value. |
| Review expired / changed | Generate a new review. Quote, identity, risk and contract checks are intentionally repeated. |
| Stop clicked but positions remain | Stop is not flatten-all. Inspect broker-reported positions/orders and use separately reviewed risk-reducing actions where supported. |
| Browser asks for token | Read `API_TOKEN` from private `axiom.env`; it is tab-scoped and intentionally not printed by the launcher. |
| Port unavailable | Set `AXIOM_PORT` before launch or close the old instance. The launcher searches its documented local port range. |
| Console closes | Start the executable from PowerShell to read the sanitized error; inspect local logs without sharing secrets. |

---

## 13. LOCAL PAPER and legacy chart

LOCAL PAPER execution remains independent of MT5: its account, orders, positions and P&L are local simulation outputs. The optional DEMO market-data connection changes only the source of prices, never the execution mode or broker state. It uses VANTAGE DEMO credentials only when the operator explicitly saves/connects that read-only feed.

**Default replay is automated.** **Start automated replay (moving_average)** and the legacy **START AUTOMATED REPLAY** control run the configured local `moving_average` strategy with automation ON. Actionable strategy signals can create simulated orders and later simulated fills without reviewing each ticket. The banner, sidebar, run panel and start result identify this behavior; it is not the `manual_only` workflow and not Daily Upstox live-market PAPER. All orders/fills remain local simulation and no broker order is routed.

**Replay audit and stop reasons are durable.** Activity / Audit records allowlisted `PAPER_SESSION_STARTED`, `PAPER_SESSION_RUNNING`, actionable `PAPER_STRATEGY_SIGNAL`, `PAPER_ORDER_SIMULATED`, `PAPER_FILL_SIMULATED`, pause/resume and `PAPER_SESSION_STOPPED` events with a run ID and per-run sequence. Normal finite replay completion reports `END_OF_DATA`; operator stop, execution-mode change, emergency stop, application shutdown and errors have distinct reasons. Immediate stop is also journaled. Provider objects, URLs, credentials, tokens, exception text and arbitrary settings are not exported. Market timestamps are data fields inside redacted payloads; event timestamps remain local audit times.

**P&L scopes are separate.** Overview's realized card is all persisted local history, while unrealized is all current local open positions. The run panel reports the current/last run's bars, actionable signals, simulated orders/fills, stop reason, realized delta, unrealized delta and ending open-position unrealized P&L. Run deltas are account-level changes from that run's start, not per-trade attribution. After process restart there is no active current run; the last completed run summary can remain visible, but persisted account P&L is not reset or presented as a new run result. These are simulated accounting values, not money earned or profitability evidence.

On **Charts**, **Start manual-only MT5 paper** requires the selected exact symbol, configured timeframe, authenticated DEMO feed, complete contract metadata, and fresh advancing quotes. It stages only newly closed bars for the always-HOLD `manual_only` strategy. Automation remains off; startup itself creates no order. A manual order still requires Review, an idempotency key, and Confirm. The saved order/fill includes quote source, source timestamp, receive timestamp and stable closed-bar provenance.

MT5 paper quantities are explicit units in the existing long-only, one-currency simulator. AXIOM does not convert MT5 lots with contract size, perform FX conversion, model short selling, or claim CFD margin/execution parity. Configured paper currency must match the DEMO account's profit currency. Fresh ticks gate entry; simulated fills use the existing last-completed-bar price/cost model, not a tick-accurate broker fill. A local paper fill based on a demo quote is **not** a Vantage demo execution fill.

- Open `/chart` for the legacy chart/replay/US-paper workflow.
- `/dashboard` is explicitly a **legacy paper/sandbox** page, not a Vantage live dashboard.
- Selecting a Vantage execution mode disconnects the independent feed and stops the legacy paper simulator so two execution sessions cannot continue as one active session.
- Paper emergency stop persists and blocks start/resume/new entries; confirmed risk-reducing exits remain supported.
- Alpaca keys in `axiom.env`, if used, are market-data credentials only. They do not enable Alpaca order routing or Vantage connectivity.

Historical Upstox sandbox/research commands in the repository are not the Vantage operator path. Do not infer live capability from those legacy components.

---

## 14. Verification and limitations

Read the exact release's `BUILD_METADATA.json` and release notes. They distinguish:

- source/unit/fake-MT5/API/UI tests;
- real-Windows DPAPI, frozen executable and installer/upgrade/uninstall checks;
- real-demo read-only market-data/local-paper evidence, if any;
- explicitly authorized demo execution connection/order evidence, if any;
- live connection/order evidence, which is not implied by any of the above.

No real-money action is part of automated development or CI. A demo account must be explicitly available for demo E2E; otherwise it is skipped and reported. Fake and demo results establish neither profitability nor live readiness.

Current functional limits include:

- Windows production credential storage and official MT5 integration; Linux fake tests are not terminal evidence.
- Manual reviewed Vantage order flow only; no Vantage strategy automation. MT5 fresh-price paper is always `manual_only` with automation off.
- MT5 paper supports the configured timeframe, one exact symbol, long-only explicit units and one account currency; no lot/contract-size or FX conversion, short selling, tick-accurate fill or multi-symbol portfolio claim.
- Market/limit entries, position close, managed-order cancel and position SL/TP modification; no claim of support for every MT5 order type.
- No TradingView relay/webhook execution.
- Local audit alerts only; no guaranteed external notification.
- Unsigned binaries unless release metadata explicitly provides signing evidence.
- Broker/session schedules, corporate actions, rejection behavior and execution quality remain external realities; software guards cannot remove market risk.

The legacy historical documentation in `README.md` preserves earlier research releases. Where it says real-money routing did not exist, that statement belongs to those historical releases, not to this Vantage-capable operator version.

---

## 15. Local real-demo market-data verification procedure

This procedure verifies a **real Vantage DEMO terminal/feed with local AXIOM simulated fills**. It does not place a Vantage demo order and does not verify live trading. Perform it yourself on Windows; never send the login, password, DPAPI files, local token or unredacted account screenshots to a developer, issue, chat or CI.

### 15.1 Prepare an isolated paper test profile

1. Hash-verify the exact unsigned Windows release and read its `BUILD_METADATA.json`. Install/open the Vantage MT5 desktop terminal using Vantage's official instructions. Manually log that terminal into the **DEMO** account you intend to test; do not select or log into a live account for this procedure.
2. Close other AXIOM instances. From PowerShell inside the extracted portable `AXIOM-Operator` folder containing the verified `AXIOM-Chart.exe` and `_internal`, choose a new empty data directory so an old paper configuration cannot conflict with the demo account:

   ```powershell
   $env:AXIOM_DATA_DIR = Join-Path $env:LOCALAPPDATA 'AXIOM-DemoFeedTest'
   .\AXIOM-Chart.exe
   ```

3. On first launch, close AXIOM with Ctrl+C in its console. Open the newly created `%LOCALAPPDATA%\AXIOM-DemoFeedTest\axiom.env` locally. Set `DEFAULT_TIMEFRAME=1m` and set `CURRENCY` to the demo account's actual profit/account currency (for example `USD` only if the account is USD). Keep `MODE=PAPER`, `LIVE_TRADING=false`, and `BROKER=LOCAL_PAPER`. `DATA_SOURCE=replay` may remain for the legacy/default chart; it is never an MT5 fallback. Restart from the same PowerShell environment.

   A saved paper database refuses a later currency/timeframe change. Do not delete or edit an existing database to bypass that guard; use a new data directory as above.

### 15.2 Connect and inspect the real feed

4. In `/terminal`, verify the global execution badge is **LOCAL PAPER**. Open **Connections**. The credential editor in this mode is explicitly DEMO-only. Enter the numeric demo login, demo password, exact demo server and terminal path locally; save/replace the protected profile. Do not use **VANTAGE LIVE**, the execution mode selector, **Test connection**, Arm or Activate.
5. Choose **Connect demo market data — read-only**. Verify provider `VANTAGE_DEMO_MT5`, masked login, exact server, account type `DEMO`, and currency. Execution must still say LOCAL PAPER, no strategy may be started by the connection, and no order may appear. A module-installed indicator or saved credential alone is not a successful feed test.
6. Open **Charts** and choose an exact symbol returned by this account. Check the broker description/path, product classification, currencies, contract size, tick/point metadata and volume limits. A share/ETF CFD must be labeled as a CFD, not owned shares. If a desired symbol is absent or unsupported, record that limitation; do not substitute a hard-coded ticker.
7. Load the chart. Required evidence is the **FRESH READ-ONLY MARKET DATA · LOCAL PAPER FILLS** label, a fresh advancing quote, separate source/receive timestamps, validated closed bars, and broker tick-volume labeling. Compare a few prices/times with the MT5 terminal. If the label says disconnected, degraded, stale, not advancing, replay or unavailable, stop and resolve that reason; do not record a pass.

### 15.3 Verify manual-only local fills and degradation

8. Choose **Start manual-only MT5 paper**. Confirm `manual_only`, the configured timeframe, automation OFF and zero automatically created orders. Enter a small **paper quantity in units, not MT5 lots**, then Review. Verify the modal says local simulated execution and shows `VANTAGE_DEMO_MT5`, source/receive timestamps and market-data ID. Confirm once; inspect the local order/fill and its provenance. The review carries an idempotency key: an API retry of that identical request must return its saved result, not create a second order. A new review is a new intention/key; never reuse a key for changed order content.
9. Create a small long paper position, then use **Disconnect demo data** (or close only the MT5 terminal for a terminal-loss check). Verify an actionable degraded/disconnected reason, no replay bars, preserved paper position/cash/order/fill state, and rejection of a new BUY. A reviewed SELL that only exits the existing long paper position remains permitted and must retain its last closed-bar provenance. Reconnect and re-verify fresh advancing quotes before another BUY.
10. Record the release commit/hash, Windows version, masked demo server/account identity, exact symbols/products tested, timeframes, timestamps, each pass/fail/skip and any terminal errors. Redact secrets and full account numbers from evidence. Keep fake-provider CI results distinct from this real-terminal check.

A pass demonstrates only the observed demo-market-data and local-simulation behavior at that time. It does not demonstrate a Vantage demo execution fill, profitability, execution quality, complete instrument coverage or real-money readiness.

---

## 16. Owner-run Vantage DEMO execution rehearsal

This separate optional procedure places **demo orders only**. It is not the market-data/paper procedure above and must never be performed with a real/live login. The account owner must run it locally on Windows. Agent and CI tests must not log into an external account or substitute fake results for this rehearsal.

1. Use a dedicated Vantage DEMO account, preferably with no external positions/orders. Verify `DEMO` in the MT5 terminal and account details before every connection. Hash-verify the exact AXIOM release and use a new empty `AXIOM_DATA_DIR` such as `%LOCALAPPDATA%\AXIOM-DemoExecutionTest`; do not overwrite an existing database.
2. Start AXIOM in its default local paper state. Save the numeric DEMO login/password and exact DEMO server in the protected profile. Explicitly select **VANTAGE_DEMO** and Apply. Confirm the three-part banner changes execution to VANTAGE DEMO, not LIVE. Choose **Test connection — read-only** and verify masked identity, server, DEMO account type and zero orders.
3. Discover and inspect the exact contracts you intend to test. Select only those symbols and save the execution selection. Set conservative, nonzero server risk limits appropriate to the demo account. Check contract size, lot min/step, currency, sessions, filling support, spread and product/CFD ownership labels.
4. Run readiness and resolve every blocker. Perform **Arm**, then separately **Activate**, with the exact DEMO phrases. Confirm both actions create zero orders and automation remains OFF.
5. On Charts, use the smallest suitable broker-lot quantity. Review every field, including indicative value versus actual MT5 margin, spread, costs limitation, quote timestamp and precheck. Type `SUBMIT DEMO ORDER` only after confirming the session is DEMO. Inspect the persisted intent and broker orders/deals after reconciliation; a click or retcode alone is not a fill.
6. If supported and appropriate for the demo test, separately review position SL/TP modification, a managed pending-order cancellation and a position-ticket-bound close. Confirm each from resulting broker position/history/deals. For an ambiguous/timeout result, do not retry: reconcile and compare the MT5 terminal.
7. Test Pause, Stop and Emergency stop. Confirm new entries block, restart returns DISARMED, and no startup/reconnect/chart/feed action creates an order. Do not expect AXIOM to cancel or flatten external/manual MT5 exposure.
8. Record release commit and binary SHA-256, Windows/MT5 builds, masked account/server, exact symbols/contracts, timestamps, risk limits (without secrets), each review/retcode/reconciled result, skips/failures and redacted performance samples. An external DEMO execution claim requires this actual evidence; otherwise record **not verified**.

A successful rehearsal verifies only the observed demo operations at that time. It does not establish live execution quality, complete broker coverage, profitability or live readiness.

---

## 17. Performance diagnostics

Open **Settings → Latency & render diagnostics** after using the relevant workflow. Empty means not measured, not zero. Server and browser p50/p95/p99 values remain separate for feed receipt, quote source age, chart backend/load/render, review, full submission, broker acknowledgement and reconciliation. Samples are bounded (512 per server metric, at most 64 server metric names, 128 per browser metric) and contain no order payloads or secrets.

For definitions, the reproducible fake-DEMO load procedure and measured development results, read `docs/TERMINAL_PERFORMANCE.md`, `docs/TERMINAL_BENCHMARK.json`, and `docs/TERMINAL_UI_BENCHMARK.json`. For a real local DEMO record, note Windows/browser/terminal builds, account environment, symbol, market/session conditions, network conditions, sample counts and timestamp. Do not compare a fake in-process acknowledgement with a real broker round trip or promise universal latency.

---

## 18. Listed shares and ETFs

Vantage share/ETF CFDs remain CFDs and do not confer underlying ownership. True listed instruments require a separate suitable provider and exact symbol + exchange/MIC + currency + asset-class identity. The current `listed-assets-v1` interface is fail-closed: no listed execution adapter or entitled listed reference/quote provider is configured, and no MT5/CFD fallback is allowed. Existing Alpaca support is read-only market data for local paper fills, not owned-share execution.

Read `docs/LISTED_ASSETS.md` for the authenticated status/policy checks, existing read-only Alpaca setup, and acceptance gates for any future provider. Do not route a listed ticker to a similarly named Vantage symbol.

---

## 19. LIVE-readiness checklist — owner only

No agent or CI process may log into LIVE, Arm/Activate LIVE, or place a real-money order. The account owner alone must decide whether to perform any real-account operation locally. If any item below is unverified, leave LIVE disconnected/disarmed and record **not ready**.

- [ ] Exact tested release commit and both Windows SHA-256 hashes verified; unsigned-binary implications understood.
- [ ] Official Vantage MT5 terminal, Windows clock, network and terminal/account permissions checked by the owner.
- [ ] Separate DPAPI LIVE profile entered locally; exact assigned server confirmed. `VantageMarkets-Live 13` is used only if Vantage assigned that server to this account.
- [ ] Read-only connection verifies masked identity, server and REAL account type. Credential presence or quote connectivity is not treated as trading permission.
- [ ] Account-discovered products/contracts/sessions/currencies/lot rules inspected; CFDs are not mistaken for owned shares/ETFs.
- [ ] Current broker positions, working orders, history and external exposure reconciled; no unresolved or ambiguous intent remains.
- [ ] Conservative nonzero risk, daily-loss/drawdown, margin, spread, slippage and freshness bounds understood and saved. Emergency stop/recovery and ambiguous-outcome procedures rehearsed on DEMO.
- [ ] Fresh readiness passes with no blockers; the owner separately Arms and Activates the intended account. Restart/reconnect/mode/chart/feed actions remain non-order actions.
- [ ] Every order receives a fresh review and exact final confirmation. Margin, spread, costs and quantity units are checked; no blind retry follows an uncertain result.
- [ ] Automation remains OFF. Monitoring, persistent emergency stop, audit retention and manual MT5 reconciliation are available.
- [ ] The owner accepts that losses, gaps, slippage, rejection, disconnection and broker/contract changes can exceed estimates.

Software tests, benchmarks, paper fills and DEMO executions do not establish profitability or LIVE readiness. Activation is an explicit owner decision, not a certification by AXIOM.

---

## 20. Research candidate generation — source CLI

The repaired source CLI can generate historical research candidates from local history. Read `docs/RESEARCH_CANDIDATES.md` (also staged as `docs/RESEARCH_CANDIDATES.md` in Windows artifacts) for commands and full limitations.

```bash
python -m app discover --instruments 'NSE_EQ|INE002A01018' --unit days --interval 1 --timeframe 1D --max-candidates 12 --output results/discovery.json
python -m app research run --instruments 'NSE_EQ|INE002A01018' --unit days --interval 1 --timeframe 1D --periods 2,3,5
python run.py --mode validate --csv data/sample.csv --symbol DEMO --timeframe 1D --output results/validation-sample
```

Instruments are evaluated separately; at least 60 candles are required per key. Validation/locked-test warmup uses only earlier candles. A baseline is never promoted merely because it is the baseline, and validation promotion requires positive expectancy and strict baseline improvement in addition to the other gates. Discovery also requires present, positive walk-forward-proxy expectancy; missing evidence cannot pass. Zero profit factor is penalized, robustness is measured against each baseline return, and invalid/non-finite router evidence fails closed.

The bundled sample CSV is synthetic. Qualified candidates, positive backtests and paper results are not guarantees of profit or LIVE readiness. Generic OHLCV research does not verify Vantage lot/tick/margin/session/CFD financing rules and never becomes an order instruction automatically. These commands do not change execution mode, Arm/Activate, automation or emergency stop. All reviewed-order and reconciliation controls remain separate.

## 21. Connecting live market data — Upstox (India) and Alpaca (U.S.)

This section connects **real market data** to the local PAPER engine for Indian
equities (Upstox) and U.S.-listed stocks/ETFs (Alpaca). Every fill stays
simulated inside AXIOM: the app contains no Upstox or Alpaca order client, and
the read-only probes below never touch an order endpoint. No profitability or
LIVE-readiness claim is made.

India and the U.S. are kept separate: different currencies (INR/USD),
calendars, credentials, instrument formats and local PAPER portfolios. A
combined view appears only when both markets are explicitly selected. When a
provider connection fails, AXIOM fails closed — it never silently substitutes
the bundled `DEMO` sample data (which is synthetic and labelled as such).

### 21.1 Upstox (India, NSE/BSE)

1. Create an Upstox developer app and obtain its API key and redirect URI.
2. Run the built-in OAuth helper to obtain a daily access token; it stores the
   token in the local `.env` (`MARKET_DATA_ACCESS_TOKEN`) with owner-only file
   permissions and never prints it:

   ```bash
   python -c "from app.data.upstox_auth import run_login_flow; run_login_flow()"
   ```

3. Configure `.env` (exact instrument keys only, `SEGMENT|ISIN` format):

   ```dotenv
   DATA_SOURCE=upstox
   MARKET_DATA_PROVIDER=upstox
   MARKET=india
   CURRENCY=INR
   MARKET_CALENDAR=nse-session
   MARKET_DATA_SYMBOLS=NSE_EQ|INE002A01018
   MARKET_DATA_TIMEFRAME=1m
   MARKET_DATA_MODE=ltpc
   MARKET_DATA_ACCESS_TOKEN=...
   ```

4. Test the connection (read-only; market-data authorize probe only):

   ```bash
   curl -X POST -H "Authorization: Bearer $AXIOM_API_TOKEN" \
     http://127.0.0.1:8000/api/connections/upstox/test
   ```

   `verdict: feed_ok` means the token is authorized for Market Data Feed V3.
   Upstox access tokens expire daily: after expiry the probe reports
   `rejected` with re-authorization guidance — rerun step 2. A `403` means the
   account is not entitled to the feed; an `unreachable` verdict means network
   connectivity failed first and nothing has been verified.
5. Start the local PAPER session from the terminal or `POST /paper/start` with
   `source=upstox`. Stop it with `POST /paper/stop`. Session state, simulated
   fills and audit events are visible in the terminal and `/paper/status`.

### 21.2 Alpaca (U.S. stocks and ETFs)

1. Create an Alpaca account and generate **market-data** API keys on the
   dashboard. AXIOM uses only read-only market-data endpoints; the keys can
   never place, modify or cancel an order through this app.
2. Configure `.env` for the U.S. market:

   ```dotenv
   DATA_SOURCE=alpaca
   MARKET=us
   CURRENCY=USD
   MARKET_CALENDAR=us-equities
   DEFAULT_TIMEFRAME=1m
   ALPACA_API_KEY=...
   ALPACA_API_SECRET=...
   ALPACA_DATA_FEED=iex
   ALPACA_SYMBOLS=AAPL,MSFT,SPY
   ```

3. **Feed entitlements are labelled exactly, never upgraded.** `iex` is the
   IEX single-venue feed (roughly 2–3% of U.S. volume) and is never described
   as the consolidated tape; `sip` is the all-exchange consolidated feed (paid
   plan); `delayed_sip` is SIP delayed 15 minutes. Set `ALPACA_DATA_FEED` to
   what your account is actually entitled to — the free Basic plan includes
   IEX only. Plan rate limits apply (Basic: 200 data calls/minute); AXIOM
   fails closed on HTTP 429 and never retries silently.
4. Test the connection (read-only latest-bar probe plus listing metadata):

   ```bash
   curl -X POST -H "Authorization: Bearer $AXIOM_API_TOKEN" \
     "http://127.0.0.1:8000/api/connections/alpaca/test?symbol=AAPL"
   ```

   The response reports the entitlement verdict, the feed label for your
   configured feed, and listing metadata (venue, class, status) for the
   ticker. `forbidden` means the credential lacks the requested feed
   entitlement; switch `ALPACA_DATA_FEED` to your entitled feed. U.S. session
   rules (09:30–16:00 America/New_York, standard holidays, 13:00 early closes,
   daylight-saving changes) are applied by the local `us-equities` calendar.
5. Start/stop the local PAPER session as in step 5 above with `source=alpaca`.

### 21.3 What "connected" means here

`POST /api/connections` lists both markets with credential-presence booleans
(secrets are never returned). Automated tests exercise these flows with mocked
provider responses and are **not** proof of real provider connectivity — the
owner must run the two connection tests above on a networked machine with real
credentials. Real-connection verification is the owner's step; without it the
connections remain BLOCKED / NOT VERIFIED regardless of how many mock tests
pass. Historical data caching, when enabled, is stored per-user locally from
the licensed provider APIs and is never committed to the repository.

## 22. U.S. brokerage path (Alpaca) — separately gated order execution

This section covers AXIOM's **broker** integration for U.S.-listed stocks and
ETFs through Alpaca's official Trading API. It is separate from market data
(section 21) and from local PAPER. Three execution destinations are strictly
distinct and always displayed before any action:

| Destination | Endpoint | Money | What fills |
|---|---|---|---|
| Local PAPER | none (in-app) | none | AXIOM's local simulator |
| **Alpaca PAPER** | `paper-api.alpaca.markets` | none | Alpaca's paper-trading environment |
| **Alpaca LIVE** | `api.alpaca.markets` | **REAL** | Alpaca brokerage |

LIVE is **disabled by default**. Configuration alone can never enable it: the
LIVE path additionally requires every readiness check to pass and two distinct
operator actions — **Arm**, then **Activate**. Disarm, any material change
(credentials, risk limits, account identity, failed reconciliation, stale or
uncertain state) or the emergency stop immediately invalidate activation. No
environment switch is ever implicit, paper credentials are never accepted for
LIVE (separate protected credential slots), and no sample/DEMO data is ever
substituted while reporting a broker connection.

### 22.1 Owner-side prerequisites (before anything works)

1. **Account eligibility — broker approval required.** Alpaca live-account
   availability depends on your country of residence and on Alpaca's approval
   of your account application (identity verification/KYC, tax forms such as
   W-8BEN, account review, funding; MFA is mandatory). See Alpaca's official
   guidance: https://alpaca.markets/learn/live-trading-account-non-us — "we
   support services to 195+ countries; if you don't see your country in the
   list, reach out to support". **Do not assume your account is eligible or
   approved until Alpaca confirms it.** AXIOM does not and cannot grant
   eligibility. A paper account does NOT imply a live account.
2. Create **separate broker API keys** in the Alpaca dashboard (paper keys for
   the paper environment; live keys only after approval). These are distinct
   from the market-data keys of section 21 and are stored separately.
3. Store the broker credentials in the protected local store (DPAPI on
   Windows; owner-only files elsewhere):

   ```bash
   python -m app.alpaca_broker.credentials paper --directory <data>/secrets
   python -m app.alpaca_broker.credentials live  --directory <data>/secrets   # only after broker approval
   ```

   Environment variables `ALPACA_BROKER_API_KEY`/`ALPACA_BROKER_API_SECRET`
   work for headless setups and are reported as `environment` (not
   `protected-store`) in status output.
4. Configure `.env` (broker fields are separate from market-data fields):

   ```dotenv
   ALPACA_BROKER_ENABLED=true
   ALPACA_BROKER_ENVIRONMENT=paper        # paper | live; live needs runtime Arm+Activate
   ALPACA_BROKER_MAX_ORDER_VALUE=1000     # all three limits are mandatory (> 0)
   ALPACA_BROKER_MAX_OPEN_POSITIONS=2
   ALPACA_BROKER_MAX_DAILY_ORDERS=10
   ```

   Missing or zero risk limits fail closed: orders are refused.

### 22.2 Operating the broker path

- `GET /api/alpaca-broker/status` — account mode, endpoint host, gate state,
  masked account, risk limits, reconciliation state. The console header shows
  the broker destination at all times (`ALPACA_PAPER · paper-api.alpaca.markets
  · gate …`, and for LIVE an unmistakable real-money badge).
- `POST /api/alpaca-broker/preview` — order preview with every check listed
  (instrument, size, risk caps, buying power, account state, gate). Nothing is
  submitted.
- `POST /api/alpaca-broker/orders` — submit. Every order carries a mandatory
  `client_order_id`; duplicates (including after a restart) are adopted or
  refused, never re-posted. Intents, submissions, fills, rejects, cancels and
  replaces are journaled in the local audit history.
- `GET /api/alpaca-broker/orders`, `DELETE|PATCH /api/alpaca-broker/orders/{id}`
  — status updates, cancellation, cancel/replace.
- `POST /api/alpaca-broker/sync-fills`, `/reconcile`, `/recover` — fill
  polling, reconciliation (any mismatch fails closed and blocks orders), and
  restart recovery (broker state is re-adopted; uncertainty blocks orders).
- `POST /api/alpaca-broker/emergency-stop` — kill switch: cancels all open
  orders at the broker and blocks every action until an explicit reset
  (which returns the gate to DISABLED; LIVE must be armed+activated again).
- LIVE only: `POST /api/alpaca-broker/gate/arm` → `/gate/activate` (and
  `/gate/disarm`). Activation issues the LIVE permit; without it a LIVE client
  cannot even be constructed.

### 22.3 What is verified and what is not

- The automated tests exercise the full order lifecycle (preview, submit,
  partial fills, completion, duplicate prevention, rejections, cancel/replace,
  reconciliation, restart recovery, kill switch) against a **mocked** paper
  broker, plus proof that tests cannot reach the LIVE endpoint. Mock tests are
  **not** proof of real broker connectivity or real fills.
- Real **Alpaca PAPER** order-flow verification requires the owner's paper
  account and network access and remains **BLOCKED / NOT VERIFIED** until run
  owner-side.
- **LIVE code paths** are tested at the gate/client level only. No real-money
  order has been placed or may be placed without broker-approved eligibility,
  funding, readiness checks passing, and explicit Arm + Activate.
- No profitability or readiness claim is made by any test, mock or CI run.

---

## 23. Unified Multi-Market Desktop Terminal (NSE INR + NASDAQ USD)

AXIOM's dark TradingView-inspired desktop terminal (`/terminal`, workspaces `01`–`09`) unifies Indian **NSE** (`INR ₹`) and US **NASDAQ** (`USD $`) operations under two strictly separated execution modes:

### 23.1 `LIVE-MARKET PAPER` (Simulated Execution on Live Market Data)
- **Data feeds**: Upstox API v2/v3 read-only quotes & historical candles for NSE (`Asia/Kolkata` IST); Alpaca Market Data API v2 (`IEX` or entitled `SIP`) for NASDAQ (`America/New_York` ET with DST, holidays, and NYSE Rule 7.2 early closes).
- **Zero broker order calls**: All orders, partial/complete fills, SL/TP triggers, and regulatory fees (Indian STT, NSE txn, SEBI, Stamp Duty, 18% GST; US SEC Section 31 & FINRA TAF) are simulated locally in isolated SQLite WAL ledgers (`INR` vs `USD`) chained with a tamper-evident SHA-256 audit log.
- **No disguised demo bars**: Outside market hours or when credentials are disconnected/expired, charts and quotes explicitly report `DISCONNECTED`, `STALE`, or `CLOSED` — sample/replay bars are never substituted as live data.

### 23.2 `LIVE-MARKET LIVE` (Default-Disabled Real-Money Execution)
- **Always OFF by default**: Startup, process restart, crash recovery (`RECOVERED_PAUSED` / halted), config import, and paper mode never enable live execution.
- **Distinct credentials & dual gate**:
  - **NSE (`UPSTOX_LIVE`)**: Requires separate `UPSTOX_LIVE_ACCESS_TOKEN`, Upstox Plus / static IPv4 order whitelist, bounded risk limits, and `/autonomy` Arm → Activate.
  - **NASDAQ (`ALPACA_LIVE`)**: Requires separate `ALPACA_BROKER_LIVE` slot, funded brokerage approval, bounded USD risk limits, fresh `GET /v2/account` + `GET /v2/clock` readiness, Arm, and exact confirmation phrase `I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS`.
- **Global Emergency Stop**: Halts NSE & NASDAQ paper sessions, Upstox Autonomy, Alpaca Broker, and Vantage sessions simultaneously while preserving protective SL/TP on open positions. Clearing requires `CLEAR EMERGENCY STOP` and leaves all live gates disarmed.

For step-by-step credential onboarding and verification status, see `docs/OWNER_SETUP_GUIDE.md` and `docs/BASELINE_AUDIT_AND_ISSUES.md`.

