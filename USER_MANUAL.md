# AXIOM Operator — User Manual

**Current source version: 3.3.0a1**

**Audience:** the account owner operating AXIOM on Windows x64.

AXIOM now has three mutually exclusive execution modes:

| Mode | Execution | What it means |
|---|---|---|
| **AXIOM PAPER** | AXIOM's local simulator | Independent paper state and clearly simulated fills. No MT5 credentials are used. |
| **VANTAGE DEMO** | Vantage MT5 demo account | Broker-executed demo orders after explicit review, Arm and Activate. Demo is not AXIOM paper. |
| **VANTAGE LIVE** | Vantage MT5 live account | Real-money capability, disabled until the account owner completes every in-app gate and confirms each order. |

> **No profitability or live-readiness claim.** Tests, a successful connection, and demo fills do not establish profitability or readiness for real money. AXIOM does not promise returns, execution quality, or a particular product offering. Live credentials and live activation are solely the account owner's responsibility.

The Vantage integration uses the official `MetaTrader5` Python package through a local MetaTrader 5 terminal. It does not scrape the Vantage website or automate the terminal with screen coordinates. Quotes, contract metadata, account state and execution for a Vantage session come from the same terminal/account connection.

For Windows download, installation and upgrade steps, read `packaging/windows/WINDOWS_INSTALL_MANUAL.md` or the copy installed beside the application. Historical Arena research and paper/sandbox material remains in `README.md`; it does not describe the Vantage order path.

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

1. Download the Windows x64 installer or portable executable and the published `SHA256SUMS.txt` from the same release.
2. Verify the exact file hash before running it. Release binaries are **unsigned unless the release metadata explicitly says otherwise**; SmartScreen can warn.
3. The per-user installer does not require administrator rights. It installs under `%LOCALAPPDATA%\Programs\AXIOM-Chart` for upgrade compatibility, while the product name is **AXIOM Operator**.
4. Alternatively, run the portable `AXIOM-Chart.exe` (the filename is retained for upgrade compatibility). It stores operator data in the per-user data directory, not beside the executable.
5. AXIOM opens `http://127.0.0.1:<port>/terminal`. The displayed local address is not a public hosted service.

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

The top mode badge and environment labels remain visible across the eight pages.

| Page | Purpose |
|---|---|
| **Overview** | Current mode, connection/lifecycle, broker or paper account state, positions, exposure and explicit limitations. |
| **Instruments** | Account-specific discovery, product/contract details, search, selection and tradability. |
| **Charts** | MT5 bars for selected Vantage instruments; the legacy chart remains available for AXIOM paper/replay. |
| **Orders / Positions** | Broker-truthful orders, deals, positions, P&L where available, durable intents, reviewed cancel/close/protection modification. |
| **Risk** | Required bounded limits, current readiness checks, Arm/Activate/Pause/Stop and durable emergency controls. |
| **Connections** | Separate demo/live credential metadata, read-only test, disconnect, replace/delete and non-secret capabilities. |
| **Activity / Audit** | Redacted local operational events and unresolved outcomes. No external alert delivery is claimed. |
| **Settings / Help** | Local token handling, links to the legacy chart/manual, mode explanations and recovery guidance. |

Controls that cannot run in the current state are disabled or return an explanatory blocker. An empty disconnected broker view is **not** a statement that the account has no positions or orders; look for `broker state unavailable` / no reconciliation timestamp.

---

## 4. Connections and credentials

### 4.1 Add a demo credential first

Use a demo account explicitly supplied by Vantage if you want to rehearse the workflow. Do not use a live login to test demo mode.

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

---

## 5. Instruments and product metadata

AXIOM calls MT5 `symbols_get` for the connected account. It does not hard-code a Vantage universe and does not infer availability from Vantage marketing pages. Entity, jurisdiction, account type and server can change the offering.

1. After a successful read-only test, open **Instruments**.
2. Search/filter the returned symbols and inspect each contract.
3. Select only the instruments you intend to use (up to 50 distinct symbols).
4. Save the selection. Changing it disarms the session and requires readiness/Arm/Activate again.

The detail view exposes available fields such as broker path/description, product classification, CFD flag, currencies, volume min/max/step/limit, contract size, tick size/value, digits/point, trade mode, sessions and filling/execution flags where MT5 supplies them.

AXIOM distinguishes forex, indices, commodities, share CFDs, ETFs/ETF CFDs and other products. **A share CFD or ETF CFD does not represent owned underlying shares or ETF units.** Classification assists display; the broker's actual contract metadata governs the order checks.

An instrument is rejected for order flow when it is unavailable, not selected, disabled/close-only for the requested action, stale, or has incomplete/inconsistent metadata needed for safe sizing and validation. A displayed symbol is not a promise that an order will be accepted.

---

## 6. Quotes and charts

Vantage quotes and execution use the same MT5 connection. Quote validation includes positive ordered prices, finite values, tick time and configured maximum age/spread. A chart's historical bars do not prove that the current quote is fresh.

To view a Vantage chart, select the instrument first, then choose a supported MT5 timeframe on **Charts**. The data label identifies DEMO or LIVE and states that it is the same MT5 account source as execution. DEMO market data can be real broker data while fills remain demo; do not confuse that with AXIOM PAPER simulation.

The independent legacy `/chart` page supports replay/US-paper workflows. Its sample CSVs are labeled sample data, and Alpaca/US-paper fills remain locally simulated. It is not a Vantage order ticket.

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
2. On **Orders / Positions**, choose a selected symbol, BUY/SELL, MARKET/LIMIT, volume and optional SL/TP. Use the displayed contract units—MT5 volume is generally lots, not a share count.
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

**Activity / Audit** records redacted events such as mode selection, credential metadata changes, connection tests, readiness/lifecycle transitions, reviews, intents, broker results, reconciliation and emergency actions. Secrets and full logins are excluded. Local event persistence is not an external notification service.

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

## 13. AXIOM PAPER and legacy chart

AXIOM PAPER remains independent of MT5. Selecting it does not use Vantage credentials. Its account, orders, positions and P&L are local simulation outputs and are labeled accordingly.

- Open `/chart` for the legacy chart/replay/US-paper workflow.
- `/dashboard` is explicitly a **legacy paper/sandbox** page, not a Vantage live dashboard.
- Selecting a Vantage mode stops the legacy paper simulator so two execution sessions cannot continue as one active session.
- Paper emergency stop persists and blocks start/resume/new entries; confirmed risk-reducing exits remain supported.
- Alpaca keys in `axiom.env`, if used, are market-data credentials only. They do not enable Alpaca order routing or Vantage connectivity.

Historical Upstox sandbox/research commands in the repository are not the Vantage operator path. Do not infer live capability from those legacy components.

---

## 14. Verification and limitations

Read the exact release's `BUILD_METADATA.json` and release notes. They distinguish:

- source/unit/fake-MT5/API/UI tests;
- real-Windows DPAPI, frozen executable and installer/upgrade/uninstall checks;
- explicitly authorized demo connection/order evidence, if any;
- live connection/order evidence, which is not implied by any of the above.

No real-money action is part of automated development or CI. A demo account must be explicitly available for demo E2E; otherwise it is skipped and reported. Fake and demo results establish neither profitability nor live readiness.

Current functional limits include:

- Windows production credential storage and official MT5 integration; Linux fake tests are not terminal evidence.
- Manual reviewed Vantage order flow only; no Vantage strategy automation.
- Market/limit entries, position close, managed-order cancel and position SL/TP modification; no claim of support for every MT5 order type.
- No TradingView relay/webhook execution.
- Local audit alerts only; no guaranteed external notification.
- Unsigned binaries unless release metadata explicitly provides signing evidence.
- Broker/session schedules, corporate actions, rejection behavior and execution quality remain external realities; software guards cannot remove market risk.

The legacy historical documentation in `README.md` preserves earlier research releases. Where it says real-money routing did not exist, that statement belongs to those historical releases, not to this Vantage-capable operator version.
