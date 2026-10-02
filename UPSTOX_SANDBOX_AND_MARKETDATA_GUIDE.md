# AXIOM / Arena — Upstox Sandbox, Market Data, and Diagnostic Manual

**Date:** 2026-10-02
**Branch:** `arena/01a0ecfa-axiom`
**Applies to:** Arena 3.0.5 backend + AXIOM desktop client

Every code citation below was re-read from source this session. Where a claim
comes from your local machine rather than this repository, it is labelled.

---

## 1. Direct answer: is AXIOM ready for live trading?

**No — and it cannot be made ready by configuration.** There is no live order
path in this codebase to enable.

Three independent, verified blocks:

**a) `LIVE_TRADING=true` does not enable trading. It prevents startup.**

```python
# app/config/settings.py:145-146  (inside __post_init__)
if self.mode.upper() != "PAPER" or self.live_trading:
    raise ValueError("Only MODE=PAPER and LIVE_TRADING=false are permitted")
```

This runs at *construction*. Setting `LIVE_TRADING=true` makes `Settings()`
raise, so nothing starts at all — not the engine, not the API, not the UI. It
is a kill switch, not a feature flag.

**b) `BROKER=LIVE` is rejected outright.**

```python
# app/config/settings.py:158-159
if broker == "LIVE":
    raise ValueError("LIVE trading is not enabled in V1")
```

Only `LOCAL_PAPER` and `UPSTOX_SANDBOX` are accepted (line 160).

**c) There is no live broker implementation — and the placeholder is
deliberately non-operational.**

The project contains four broker classes, and none can place a live order:

| Class | Location | Status |
|---|---|---|
| `BrokerInterface` | `app/paper_trading/broker.py:18` | Abstract base — cannot be instantiated |
| `PaperBroker` | `app/paper_trading/broker.py:40` | The only functional broker; simulated fills |
| `UpstoxSandboxBroker` | `app/brokers/sandbox.py:173` | Sandbox orders only; never a live host |
| `FutureRealBroker` | `app/paper_trading/broker.py:33` | **Non-operational marker** |

`FutureRealBroker` is blocked twice over. Its constructor raises:

```python
# app/paper_trading/broker.py:36-37
def __init__(self, *args, **kwargs):
    raise RuntimeError("Real-money trading is not implemented or permitted")
```

and it is *also* still abstract, so instantiating it fails before that line is
even reached. Verified empirically:

```
>>> FutureRealBroker()
TypeError: Can't instantiate abstract class FutureRealBroker with abstract
methods cancel_order, get_balance, get_order, get_orders, get_positions,
place_order
```

It contains no HTTP client, no order URL, and no SDK import — there is no
network path in the file at all. Its only references in the codebase are two
tests asserting it stays broken
(`tests/test_execution.py:145`, `tests/test_upstox_live.py:587`,
`test_real_broker_remains_non_operational`).

The module docstring at `app/paper_trading/broker.py:1` states it plainly:
"Paper is the only implemented broker. No SDK or real order URL exists."

### What this means practically

There is no "step-by-step guide to enable live trading" to write, because the
step does not exist. Producing one would require writing a live execution
broker from scratch against a real-money endpoint — in a system whose order
reconciliation you have correctly identified as a temporary patch (§5), and
whose market data feed does not yet authenticate (§4).

I am not going to do that. It would put real capital at risk on an
unreconciled order lifecycle. This is a refusal on engineering grounds, not a
policy technicality: **you cannot safely trade live on a system that cannot
tell you what it owns.**

The realistic ceiling for this codebase today is **Sandbox execution + paper
risk**, which is what it was built for.

---

## 2. The two-credential model (the root of most confusion)

These are **different secrets for different endpoints**. One never substitutes
for the other.

| Variable | Endpoint it authorizes | Host |
|---|---|---|
| `UPSTOX_SANDBOX_ACCESS_TOKEN` | Sandbox **order** APIs (V3) | `https://api-sandbox.upstox.com/v3` |
| `MARKET_DATA_ACCESS_TOKEN` | Market Data Feed **V3 authorize** | `https://api.upstox.com/v3/feed/market-data-feed/authorize` |

Your log confirms this split empirically: the sandbox token authenticated
against the sandbox order API (HTTP 404 / `UDAPI100010` = "order not found" =
authenticated, no order placed) while the market-data authorize endpoint
returned 401. **Both results are consistent and neither is a bug in Arena.**

The authorize URL is a coded default (`app/data/upstox_stream.py:52`):

```python
AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
```

Note the host: `api.upstox.com`, **not** `api-sandbox.upstox.com`. Market data
has no sandbox host. Sending a sandbox token there will always 401.

### Precedence, verified

`app/config/settings.py:252-254` defines exactly two aliases:

```python
aliases = {
    "market_data_access_token": "UPSTOX_ACCESS_TOKEN",
    "market_data_symbols": "UPSTOX_INSTRUMENT_KEYS",
}
```

Resolution order (`settings.py:259-261`): read `MARKET_DATA_ACCESS_TOKEN`
first; only if it is **absent** fall back to `UPSTOX_ACCESS_TOKEN`. So an
explicit `MARKET_DATA_ACCESS_TOKEN` always wins — adding `UPSTOX_ACCESS_TOKEN`
will not override it. Your instinct on this was correct.

---

## 3. A real defect in the shipped defaults

Your local discovery is a genuine correction to this repository:

| Field | Repo default | Correct for Sandbox V3 |
|---|---|---|
| `upstox_sandbox_base_url` | `https://api-hft.upstox.com` (`settings.py:101`) | `https://api-sandbox.upstox.com` |
| `upstox_sandbox_order_history_url` | `https://api.upstox.com/v2/order/history` (`settings.py:102`) | no V2 equivalent on the sandbox host |

That second row is the actual cause of your reconciliation 401: the shipped
default points the **V2 production** history endpoint at a **sandbox** token.
Your temporary patch treats the symptom; the default is the disease. See §5.

Both are overridable from `.env` without touching source:

```
UPSTOX_SANDBOX_BASE_URL=https://api-sandbox.upstox.com
```

---

## 4. Diagnosing the market-data 401

Run this from the Arena root (`F:\arena_3.0.5_fixed (1)\trading_bot`). The
probe lives **outside** the repo so it cannot modify anything.

```powershell
# Stage 1 - offline, no network
python C:\path\to\market_data_probe.py

# Only if stage 1 looks sane
python C:\path\to\market_data_probe.py --probe
```

It never prints a token. It prints an 8-hex SHA-256 fingerprint and a length,
which is enough to answer the only question that matters here.

### Reading stage 1

**If it reports the two fingerprints are identical**, you have the sandbox
token in `MARKET_DATA_ACCESS_TOKEN`. That fully explains the 401 and the fix
is to put a genuine market-data credential there.

**If the fingerprints differ**, the copy-paste theory is excluded and the
market-data token itself lacks Feed V3 scope or is expired. That is a Upstox
console matter, not an Arena one.

I verified all three branches here (identical, different, absent), and
confirmed zero occurrences of the secret values in the output.

### Reading stage 2

Stage 2 issues one bounded `GET` to the authorize endpoint — no WebSocket, no
order. Interpretation follows `verify_feed_credential`
(`app/data/upstox_auth.py:89`):

| Result | Meaning |
|---|---|
| HTTP 200 **with** a `wss://` `authorized_redirect_uri` | **Feed OK** — safe to open the WebSocket |
| HTTP 200 without `wss://` | Unexpected shape; treated as not verified |
| 401 / 403 | Token lacks Feed V3 scope, or expired |
| 429 / 5xx / transport error | **Not** a credential verdict — retry |

The `wss://` check matters: a bare 200 is not sufficient. The code requires
the redirect URI before it will connect.

### On the Analytics Token

Your log says the Analytics Token route is next. Two things to check before
blaming the token:

1. Confirm the value actually landed in `MARKET_DATA_ACCESS_TOKEN` — stage 1
   shows the fingerprint and length without exposing it.
2. Confirm it differs from the sandbox token's fingerprint.

If both hold and you still get 401, the token does not carry Feed V3
entitlement. Per your `.env.example` notes, the documented path for Feed V3 is
the standard OAuth login token; the Analytics Token's "Websocket" category is
listed in current docs but is not equivalent.

### OAuth status

`run_login_flow` exists in `app/data/upstox_auth.py`, but you verified
`UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET`, and `UPSTOX_REDIRECT_URI` are all
absent. **OAuth has not been configured or completed** — do not describe it as
an available fallback until those three exist and a token has been obtained.

---

## 5. The reconciliation gap (do not paper over this)

Your temporary patch:

```python
if self._history_url.startswith("https://api.upstox.com/"):
    logger.info("SANDBOX_RECONCILE_SKIPPED")
    return []
```

This is a reasonable *stopgap* — it prevents a sandbox token leaking to the
production history endpoint. But `return []` means **reconciliation silently
reports "nothing happened" forever.** Submitted orders would never resolve to
fills, and the ledger would drift from reality without any error.

I confirmed this patch is **not** in the repository — it is a local edit only.

The correct fix is architectural, not a guard clause:

1. Give the sandbox broker a **sandbox** history endpoint, or an explicit
   "no remote history available" mode that is *loud*, not silent.
2. If sandbox history is genuinely unavailable, orders must resolve to an
   explicit `UNRECONCILED` terminal state that surfaces in the UI and blocks
   further submission — never an empty list that reads as success.
3. Add a regression test asserting that a skipped reconciliation is
   distinguishable from a genuinely empty one.

Until that exists, `python -m app sandbox-test --place-order` should stay off,
exactly as you concluded. An order you cannot reconcile is worse than no order.

### 5a. What the shipped code actually does today (measured, not inferred)

The repository has **no** `return []` stopgap. Its real behaviour is different,
and the difference matters. Reproduced against the shipped defaults with a mock
transport (sandbox `base_url`, production `history_url`) and one tracked order:

```
available before: True
reconcile() raised SandboxAuthError: Sandbox credential rejected (HTTP 401); broker disabled
available after : False
history call made: True   -> GET api.upstox.com/v2/order/history

later ORDER call on the valid sandbox host:
   SandboxAuthError: Sandbox broker disabled after an auth failure
```

`_request` fails closed **globally, not per endpoint** (`app/brokers/sandbox.py`,
quoted verbatim):

```python
        if response.status_code in {401, 403}:
            self.available = False  # fail closed on auth errors
            logger.error("SANDBOX_AUTH_FAILURE", extra={"http_status": response.status_code})
            raise SandboxAuthError(
```

`self.available = False` is not scoped to the endpoint that failed. `_request`
opens with `if not self.available: raise SandboxAuthError("Sandbox broker
disabled after an auth failure")`, so once any single call is rejected, every
later call is refused too.

So a 401 from the *history* endpoint — which is pointed at a **production** host
by the shipped default, `upstox_sandbox_order_history_url =
"https://api.upstox.com/v2/order/history"` (`app/config/settings.py:102`) — permanently
disables the **entire** broker, including the order endpoints on the perfectly
valid sandbox host. Reconciliation is what kills order placement, not the
credential.

Note the shipped defaults are inconsistent with each other: `base_url` is
`https://api-hft.upstox.com` (line 101) but history is `api.upstox.com` (line 102).
A sandbox token can never authorise the latter.

Two caveats worth stating precisely:

- `reconcile()` iterates `self.orders`. With **no** tracked order it returns
  without making any history request at all, so this failure only surfaces once
  an order has actually been submitted — the worst possible moment to discover it.
- The host allow-list added in commit `1d34479` covers `base_url` only.
  **`history_url` is still unvalidated**, so the production default above is
  still accepted at construction. Closing that is the remaining gap; it needs a
  decision on what the correct sandbox history endpoint is, which is not
  documented, so it has not been guessed at here.

This is the concrete reason `--place-order` must stay off: under the shipped
defaults the first reconciliation attempt disables the broker outright.

---

## 6. Common problems and fixes

| Symptom | Real cause | Fix |
|---|---|---|
| `Only MODE=PAPER and LIVE_TRADING=false are permitted` | `LIVE_TRADING=true` or `MODE` ≠ `PAPER` | This is intended. Live trading does not exist here. |
| `BROKER must be LOCAL_PAPER or UPSTOX_SANDBOX` | `BROKER=LIVE` | Use `UPSTOX_SANDBOX`. |
| `BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN` | Token absent, or `.env` not loaded | See the two rows below. |
| Token is in `.env` but "not set" | `sandbox-test` reads `os.getenv` at `app/__main__.py:518`, ten lines **before** `_load_settings()` at line 528 runs `load_dotenv` | Export it for the session, or reorder those lines. Known defect. |
| `.env` ignored entirely | `python-dotenv` missing → `except ImportError: pass` at `settings.py:241` | `pip install python-dotenv`. Silent failure, no warning. |
| `.env` seems overridden | `load_dotenv(..., override=False)` — real env vars win | Clear the machine-level variable. |
| `MARKET_DATA_TIMEFRAME must equal DEFAULT_TIMEFRAME` | The two differ (`settings.py:208`) | Set both to `1m`. |
| Instrument rejected | Internal-symbol handling | `NSE_EQ\|INE002A01018` works; `NSE_INDEX\|Nifty 50` does not. |
| Sandbox 401 on order APIs | Wrong host — repo default is `api-hft.upstox.com` | `UPSTOX_SANDBOX_BASE_URL=https://api-sandbox.upstox.com` |
| Sandbox 404 + `UDAPI100010` | **Not an error** — authenticated, order id absent | This is the success signal. |
| Market-data 401 | Wrong token type, or sandbox token in the market-data slot | §4 |
| 401 that repeats identically | Broker latches `available=False` after any 401/403 | Restart the process; fix the cause first. |
| `{"detail":"Set API_TOKEN to enable protected API endpoints"}` | Backend reachable, endpoint protected | Set `API_TOKEN` in Arena's `.env`. |
| UI `KeyError: 'Logs'` | Local source edits to `desktop/app.py` | Inspect before editing; backend correctness first. |

---

## 7. Proven vs unproven

Your own §14/§15 split is accurate and I am not adjusting it upward.

**Proven:** environment and dependencies; `.env` loading; sandbox credential
loads; correct sandbox host; sandbox V3 reachable; sandbox credential
authenticated; no order placed during probing; instrument validation;
`DATA_SOURCE=upstox` accepted; timeframe mismatch fixed; protected API
mechanism identified; local `API_TOKEN` generated.

**Not proven:** Market Data Feed V3 credential accepted; WebSocket connected;
live candles normalized; full strategy pipeline on external data; the
risk → order → fill → reconciliation cycle; robust sandbox reconciliation;
production readiness; **real-money live trading readiness**.

The single current blocker is the market-data 401. Everything downstream of it
is unverifiable until it clears.

---

## 8. Recommended order of work

1. Run stage 1 of the market-data probe. Compare fingerprints (§4).
2. If they match, install a genuine market-data credential in
   `MARKET_DATA_ACCESS_TOKEN`; if they differ, resolve Feed V3 entitlement with
   Upstox.
3. Re-run with `--probe`. Require `feed_ok` with a `wss://` redirect.
4. Only then test the WebSocket stream, and require `frames > 0` and
   `candles > 0`.
5. Replace the reconciliation stopgap with a loud `UNRECONCILED` state (§5).
6. Then run controlled Sandbox end-to-end tests.

Live trading is not step 7. It is not on this list.

---

## 9. Safety invariants this guide assumes

- No secret value is printed, requested, or logged at any point.
- No order, cancel, or modify is issued by any diagnostic here.
- Sandbox diagnostics use `GET` only.
- Arena source is not modified by any probe; the probes live outside the repo.
- `.env` is gitignored and must never be committed.

### 5b. Scoping the fix (measured, two attempts)

The correct shape is known: stop the broker sending a sandbox credential to a
non-sandbox history host, and take the honest unresolved path instead. The
ordering matters — the guard must test `self.available` FIRST and re-raise, or
it bypasses `_request`'s own `if not self.available: raise` and silently masks
fail-closed. With that ordering, verified behaviour was:

```
1) credential reached production host: False
   broker still available          : True
   order endpoint works            : True
2) fail-closed preserved          : Sandbox broker disabled after an auth failure
```

So the guard itself is sound. What makes it a refactor rather than a patch is
blast radius: **26 tests in 4 files** construct brokers on the shipped
production history default and rely on reconciliation reaching it —
`test_v3_0_2_hardening.py` (12), `test_v3_0_1_execution_safety.py` (9),
`test_v1_separation.py` (4), `test_v1_engine.py` (1). Each needs its broker
pointed at a sandbox history host and its mock taught to route that host, the
way `tests/test_v1_sandbox.py` was updated.

Note also that `upstox_sandbox_order_history_url` is load-bearing for the
diagnostic, not just for reconciliation: `app/__main__.py:562` issues
`GET <history_url>?order_id=0` and derives the credential verdict from the
status code. Blanking the default therefore breaks `sandbox-test` outright, so
the default must stay and the guard belongs in the broker.

Left undone deliberately rather than shipped half-applied.

### 5c. Two implementation traps found while attempting the guard

1. **Argument order.** Several test constructions are single-line with
   positional args first — `UpstoxSandboxBroker("tok", INSTRUMENT, transport=...)`.
   Inserting `history_url=` immediately after the opening paren puts a keyword
   argument before positionals and is a `SyntaxError` at collection time, which
   aborts the whole run rather than failing one test. Any scripted insertion
   must go last, before the closing paren.
2. **Mock routing differs per file.** `test_v3_0_2_hardening.py:79` matches
   history by *path* (`/v2/order/history`), so a host change is invisible to it.
   `test_v1_separation.py:31` and `test_v1_engine.py:37` match by *host*, and
   `test_v1_separation.py:65` asserts the exact host set — all three need
   updating together.

The cleaner shape this points to: `upstox_sandbox_order_history_url` currently
does double duty — broker reconciliation *and* the `sandbox-test` credential
probe (`app/__main__.py:562`). Splitting those into two settings would let the
broker default to a sandbox host without breaking the diagnostic, and would
reduce the test churn to the two host-matched files.
