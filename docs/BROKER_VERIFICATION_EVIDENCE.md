# Broker verification evidence — read-only attempt, 2026-11-07

Dated record of what was actually run when asked to execute the owner-side
preflight. It exists so no later reader has to infer, from a green test suite,
that a broker was contacted.

Branch at the time: `arena/8d3d005e-axiom` @ `2a36aa9` (local HEAD == remote tip,
clean tree before the run).

## 1. Credential availability — none, so no provider calls were made

Checked for **presence only**; no value was read, printed, copied or persisted.

| Location | Check | Result |
| --- | --- | --- |
| Environment | `AUTONOMY_LIVE_ENABLED`, `UPSTOX_LIVE_ACCESS_TOKEN`, `UPSTOX_ALGO_NAME`, `AXIOM_UPSTOX_EXPECTED_PUBLIC_IP` | all `ABSENT` |
| Environment | `ALPACA_BROKER_ENABLED`, `ALPACA_BROKER_API_KEY`, `ALPACA_BROKER_API_SECRET`, `ALPACA_BROKER_ENVIRONMENT` | all `ABSENT` |
| Environment | `ALPACA_MARKET_DATA`, `UPSTOX_ACCESS_TOKEN`, `ALPACA_BROKER_LIVE` | all `ABSENT` |
| DPAPI store | `%LOCALAPPDATA%\AXIOM` — Windows-only by construction | not applicable on this host (`LOCALAPPDATA` unset, `os.name != "nt"`) |
| File store | `$XDG_DATA_HOME/AXIOM` / `~/.local/share/AXIOM` (`secrets/`) | directory **absent** |
| `.env` | `~/.env`, `<repo>/.env` | neither exists |

Because no credential was configured in any approved store, **no Upstox or
Alpaca endpoint was contacted** — not identity, not permissions, not positions,
not a quote. That is the intended fail-closed behaviour, and it is the blocker
for this item: the read-only live checks could not be attempted at all.

The readiness CLI still ran, because it is local-only and calls no broker:

```
python -m app live-readiness          # exit 0
python -m app live-readiness --json   # exit 0
```

Result: both markets `PAPER`, gate `DISABLED`, `all_markets_live_enabled=false`,
`blocking_markets=[NSE, NASDAQ]`, every `credential_requirements[].configured`
false, `verification.current_level="MOCK"`, `order_path_verified=false`,
`coverage={"mock_or_fake_transport_only": true, "live_verified": false,
"windows_acceptance": "NOT_RUN"}`. A scan of the serialised report found **0**
secret-shaped values.

## 2. EDIS — checked against the documented API surface

Upstox documents EDIS authorisation as a one-time action in the **web / iOS /
Android** app, required for any GTT order with a SELL leg, and states it then
applies to API orders:

> "To place GTT orders with a SELL leg, EDIS authorization is required. You can
> authorize EDIS from any of our platforms—Web, iOS, or Android by placing a GTT
> order. Once authorized on any one platform, it will also be valid for
> API-based orders."
> — `https://upstox.com/developer/api-documentation/place-gtt-order/`

The only documented holdings read,
`GET /v2/portfolio/long-term-holdings`, returns `isin`, `cnc_used_quantity`,
`collateral_type`, `haircut`, `product`, `quantity`, `trading_symbol`,
`last_price`, `close_price`, `pnl`, `day_change`, `day_change_percentage`,
`instrument_token`, `average_price`, `collateral_quantity`,
`collateral_update_quantity`, `t1_quantity`, `exchange`. **No EDIS / DDPI
authorisation field exists**, and no other documented endpoint exposes one.

Conclusion: EDIS state **cannot** be confirmed through an officially supported
read-only API. Anything depending on it stays disabled rather than trusting an
operator attestation. Recorded in `NSE_EXIT_BLOCKERS`, the capability profile,
the preflight step `edis_and_settlement_state`, and §3 of
`docs/LIVE_EXECUTION_READINESS.md`.

## 3. Windows acceptance — NOT RUN

`uname -a` → `Linux e2b.local 6.1.158+ ... x86_64 GNU/Linux`; `wine`, `wine64`,
`pwsh`, `powershell` and `makensis` are all absent. No Windows build, install or
packaged-CLI check was executed, and no Linux run is substituted. The unexecuted
commands are listed in `docs/WINDOWS_ACCEPTANCE_STATUS.md`.

## 4. Where this leaves each broker

| | Upstox / NSE | Alpaca / US |
| --- | --- | --- |
| Mock coverage | yes (deterministic fakes) | yes (deterministic fakes) |
| Paper / sandbox | no | no (paper host never contacted) |
| Read-only live connectivity | **not attempted** — no credential | **not attempted** — no credential |
| Windows acceptance | NOT RUN | NOT RUN |
| Order-path verification | none | none |
| Exit route | disabled (`exit_route_unsupported`) | routed, close-only behind the held-long guard, never exercised |
| Live execution | disabled, not armed | disabled, not armed |

No order was submitted, modified, cancelled or closed. No gate was armed or
activated. Nothing here supports a claim of unrestricted live readiness.
