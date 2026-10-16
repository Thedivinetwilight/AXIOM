# Verification notes — revisions named “V0.6.1”

> **Historical version report:** these V0.6.1 notes are retained for release history and do not describe the current AXIOM 3.2.0 source line. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md).


Two different pieces of work share the V0.6.1 label; both were fully executed:

1. **Verification revision of the unchanged V0.6 code** (75 pytest + 61
   real-process/local-network integration assertions, 0 failures; Termux and
   external market data explicitly unverified). See `../verification_v061/`
   in the delivery workspace (REPORT.md, COMMANDS_EXECUTED.md, evidence archive
   `trading_bot_v0.6.1_verification.zip`).

2. **V0.6.1 real market-DATA integration** (Upstox V3 read-only feed into the
   unchanged paper pipeline) — this is the current code. See
   [V061_VERIFICATION.md](V061_VERIFICATION.md) for what was verified,
   what is explicitly NOT verified (authenticated external feed, Termux), and
   the honest limitations.

The application remains PAPER ONLY in both.
