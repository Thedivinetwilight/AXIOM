# AXIOM Sandbox Readiness

Assessment date: 2026-10-16 (UTC). Branch `arena/6f2d964a-axiom`.
Scope: Upstox **SANDBOX** only. No production-order endpoint was probed, no
live credential exists or was used, no request in this session ever targeted
a production host with a sandbox credential.

---

## 1. Provider contract (verified against primary sources, 2026-10-16)

| Source | Finding |
|---|---|
| Official docs — *Sandbox enabled APIs* | Place Order, Place Order V3, Place Multi Order, Modify Order, Modify Order V3, Cancel Order, Cancel Order V3. **No order-history/details/trades/orderbook endpoints.** Sandbox tokens are "exclusively for sandbox orders" and cannot be used for live transactions. |
| Official docs — API overview (page age Jun 2026) | "Sandbox … currently supports place, modify, and cancel order APIs." |
| Official `upstox-python` SDK (`Configuration.sandbox_urls`) | Sandbox mode pins **every** host to `https://api-sandbox.upstox.com` with a 7-path allowlist; any other API raises "This API is not available in sandbox mode." |
| Official docs — Enhanced URL announcement | `api-hft.upstox.com` is the **live** place/modify/cancel host. |
| Official docs — Get Order History | `GET https://api.upstox.com/v2/order/history` (v2), not marked sandbox-enabled. |
| Official docs — Sandbox announcement (2024) | **Conflicts** with the list above: claims sandbox orders stay active 24h with retrieval of "order details, orderbook, and historical records … same fidelity as in a live environment." This is the sole source suggesting sandbox history reads exist. |
| Community threads (2025) | Sandbox = request/response simulation; only listed endpoints supported. |
| Third-party auth doc | Sandbox access token: 30-day lifetime, scope "sandbox orders only". |

**Conclusion:** order-history reads on the sandbox are NOT a documented
capability; one official announcement claims otherwise. The code therefore
must (a) never let a sandbox token reach a production host, and (b) treat the
read route as a per-capability question with an honest unknown state.

## 2. What the code now guarantees (locally verified)

1. **Host boundary.** `UPSTOX_SANDBOX_ORDER_HISTORY_URL` defaults to
   `https://api-sandbox.upstox.com/v2/order/history`; order mutations default
   to `https://api-sandbox.upstox.com`. A production or unknown history host
   is refused at `Settings` validation **and** at broker construction —
   environment variables cannot bypass it. The live broker keeps the
   production history host (no over-correction).
2. **Per-capability history state.**
   `status_view()["history_capability"]` reports
   `unknown|available|unsupported|auth_rejected|rate_limited|provider_error|unexpected`
   with the HTTP reason and a transition event `SANDBOX_HISTORY_CAPABILITY`
   (never contains the token or query). A 404/405 without `UDAPI100010` ⇒
   `unsupported` (documented capability boundary). This state is recorded
   **before** the auth branch, so an unsupported read can never masquerade as
   a credential verdict.
3. **Order availability is independent of the read side.** Unsupported or
   failing history reads never clear `available` (which gates order
   mutations). A 401/403 **from the sandbox environment itself** still fails
   the whole broker closed — the same token guards the order endpoints, so
   that is genuine credential evidence.
4. **Honest degradation.** Orders remain acknowledged-but-unconfirmed when
   remote status cannot be read; the submission journal blocks blind
   resubmission; unresolved intents conservatively block new risk-increasing
   entries (exits never blocked).
5. **CLI verdict semantics** (`python -m app sandbox-test`):
   - exit 0 — credential accepted, or one small sandbox order placed
     (`credential_verified_by="authenticated order placement"`);
   - exit 4 — `probe_unverified_capability` / `order_history_not_exposed`:
     credential **UNVERIFIED** (missing read route), with next steps;
   - exit 1 — credential rejected (401/403) or transient/unexpected error;
   - exit 2 — configuration refused / live mode;
   - exit 3 — no token set. Nothing attempted, nothing claimed.
   The summary prints `mode`/`order_host`/`history_host` and never a token.
   401/403 responses never trigger `--place-order`.

Enforcing tests (all green this session):
`tests/test_sandbox_history_capability.py` (22 tests), host assertions in
`tests/test_v1_separation.py`, `tests/test_v1_sandbox.py`,
`tests/test_v1_engine.py`, adversarial edges in
`tests/test_adversarial_fail_closed.py`.

## 3. Gate E status: BLOCKED (honest)

- **What ran:** full mock/contract verification of the sandbox surface
  (place/modify/cancel/reconcile/lookup/capability/host boundary/CLI), plus
  mutation checks on the safety net (see `AXIOM_COMPLETION_LEDGER.md` §3).
- **What did NOT run:** any request against `api-sandbox.upstox.com`. This
  sandbox has no `UPSTOX_SANDBOX_ACCESS_TOKEN` (none was supplied, none
  exists in the workspace, and inventing one is forbidden). Provider
  documentation was reachable only through web search from this environment.
- **Rule applied:** a mock pass never satisfies Gate E. Sandbox readiness
  against the real provider is **UNVERIFIED — blocked on credentials**, not
  failed.

## 4. What an operator with a real sandbox token should run

```bash
# 1. read-only probe (exit 0/1/3/4 semantics above)
python -m app sandbox-test

# 2. if exit 4 (read route not exposed): verify the credential with one small order
python -m app sandbox-test --place-order

# 3. record the observed verdict
python -m app sandbox-test --json   # machine-readable; no token inside
```

Then (optionally) run the bounded engine against the sandbox:

```bash
# .env: BROKER=UPSTOX_SANDBOX, UPSTOX_SANDBOX_ACCESS_TOKEN=…, DATA_SOURCE=upstox
python -m app initialize
python -m app engine --max-candles 500
```

Whatever `history_capability` reports (supported or unsupported) matches the
documented contract handling above; both answers are safe. Paste the result
into this file's §5.

## 5. Observed live-sandbox verdict

_(to be filled by an operator/session holding a real sandbox token)_

- Date:
- `sandbox-test` exit code:
- `history_capability.state`:
- Order placement attempted (y/n):
- Notes:
