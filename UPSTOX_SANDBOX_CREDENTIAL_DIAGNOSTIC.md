# Upstox Sandbox Credential Diagnostic — Session Report

**Date:** 2026-10-01
**Branch:** `arena/01a0ecfa-axiom` at `a921b8e` (local == remote, tree clean)
**Scope:** read-only investigation of how `UPSTOX_SANDBOX_ACCESS_TOKEN` reaches
the AXIOM process, plus a safe procedure for creating a local `.env`.

No source was modified. No `.env` was committed. No secret value was printed,
requested, or logged. No order or write operation was issued.

---

## 1. Starting point

Your Windows machine reported:

- Python 3.13.15, `python-dotenv` 1.2.3
- No `.env` anywhere under `F:\AXIOM-source-1.0.0`
- All five credential variables absent (`UPSTOX_SANDBOX_ACCESS_TOKEN`,
  `UPSTOX_ACCESS_TOKEN`, `MARKET_DATA_ACCESS_TOKEN`, `UPSTOX_CLIENT_ID`,
  `UPSTOX_CLIENT_SECRET`)

Conclusion accepted: **no Upstox credential is currently available to the AXIOM
process.** That matches what I found in this sandbox — all four token variables
absent, not empty.

---

## 2. The configuration mechanism (verified)

`app/config/settings.py` is the single source of truth.

```python
ROOT = Path(__file__).resolve().parents[2]      # line 9  -> repository root

@classmethod
def from_env(cls):
    try:
        from dotenv import load_dotenv          # line 238
        load_dotenv(ROOT / ".env", override=False)   # line 240
    except ImportError:
        pass
```

Four consequences, each checked against source:

1. **Variable name is exactly `UPSTOX_SANDBOX_ACCESS_TOKEN`** — derived by
   uppercasing the dataclass field name (`settings.py:259`,
   `raw = os.getenv(name.upper())`). There is **no alias**: the alias table
   (`settings.py:252-254`) covers only `market_data_access_token` →
   `UPSTOX_ACCESS_TOKEN` and `market_data_symbols` → `UPSTOX_INSTRUMENT_KEYS`.
2. **`.env` must sit at the repository root**, resolved from `ROOT` — not from
   the shell's working directory.
3. **`override=False` means real environment variables win over `.env`.** A
   stale machine-level variable silently defeats a correct `.env`.
4. **`repr=False` on the field** (`settings.py:100`) keeps the token out of
   reprs, so it will not leak into a traceback or debug print.

### Silent failure mode

`load_dotenv` is wrapped in `except ImportError: pass`. If `python-dotenv` is
missing, `.env` is skipped **with no warning** and only exported environment
variables work. Confirmed here: with `.env` present and dotenv absent,
`Settings` resolved nothing; after installing dotenv, the same file resolved
correctly. Your machine has 1.2.3, so this does not apply to you — but it is
the first thing to check on any other machine.

---

## 3. Defect found: `sandbox-test` cannot see a `.env`-only token

`cmd_sandbox_test` reads the credential **before** `.env` is ever loaded:

```
app/__main__.py:507   def cmd_sandbox_test(args) -> int:
app/__main__.py:518       token = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
app/__main__.py:528       settings = _load_settings()   # <- this runs load_dotenv
```

`load_dotenv` lives only inside `Settings.from_env()` (`settings.py:238-240`),
which is reached at line 528 — ten lines *after* the token is read at 518.

### Proof (re-run this session)

A throwaway `.env` containing a 25-character dummy token, with the process
environment deliberately unset:

```
A) Settings.from_env()  ->  present: True   len: 25
B) python -m app sandbox-test
     "performed": false,
     "reason": "UPSTOX_SANDBOX_ACCESS_TOKEN not set; nothing attempted, nothing claimed"
```

Same file, same interpreter, two contradictory answers. The engine path (which
reads `settings.upstox_sandbox_access_token`) works; the diagnostic path does
not.

**Not patched** — you instructed no source modification. It is a one-line
reorder and worth filing separately. The procedure in §5 works around it.

---

## 4. Required sandbox fields

From `app/config/settings.py`, cross-referenced with `.env.example`:

| Env var | settings.py | .env.example | Required |
|---|---|---|---|
| `UPSTOX_SANDBOX_ACCESS_TOKEN` | 100 | 60 | **Yes — the only hard requirement** |
| `BROKER` | 99 | 57 | Must be `UPSTOX_SANDBOX` to activate |
| `UPSTOX_SANDBOX_BASE_URL` | 101 | 62 *(commented)* | No — defaults to `https://api-hft.upstox.com` |
| `UPSTOX_SANDBOX_ORDER_HISTORY_URL` | 102 | 63 *(commented)* | No — defaults to `https://api.upstox.com/v2/order/history` |
| `SANDBOX_PRODUCT` | 103 | 64 | No — default `I`, validated ∈ `{I, D, MTF}` |
| `SANDBOX_ORDER_TAG` | 104 | 65 | No — default `arena-v1` |

The gate is narrow (`settings.py:162-164`):

```python
if broker == "UPSTOX_SANDBOX" and not self.upstox_sandbox_access_token.strip():
    raise ValueError(
        "BROKER=UPSTOX_SANDBOX requires UPSTOX_SANDBOX_ACCESS_TOKEN in the environment")
```

Fail-closed guards that also abort startup: `MODE=PAPER` + `LIVE_TRADING=false`
(line 146); `BROKER` ∈ `{LOCAL_PAPER, UPSTOX_SANDBOX}`, `LIVE` rejected (161);
`SANDBOX_PRODUCT` ∈ `{I, D, MTF}` (166).

Leave both URL lines **commented** — the coded defaults are already correct.

---

## 5. Minimal safe procedure

From `F:\AXIOM-source-1.0.0\AXIOM`:

```powershell
# 1. Create from template (no secrets involved yet)
Copy-Item .env.example .env

# 2. Edit .env; change ONLY these two lines
#      BROKER=UPSTOX_SANDBOX
#      UPSTOX_SANDBOX_ACCESS_TOKEN=<paste sandbox token>
#    Leave MODE=PAPER and LIVE_TRADING=false untouched. Save UTF-8, no BOM.

# 3. Confirm Git will never track it
git check-ignore -v .env      # must print .gitignore:1:.env  .env
git status --short            # .env must NOT appear

# 4. Verify resolution WITHOUT printing the value
python -c "from app.config.settings import Settings; t=Settings.from_env().upstox_sandbox_access_token; print('present:', bool(t.strip()), 'len:', len(t))"

# 5. ONLY if step 4 printed True — load into session without echoing, then probe
$env:UPSTOX_SANDBOX_ACCESS_TOKEN = ((Get-Content .env | Where-Object { $_ -match '^UPSTOX_SANDBOX_ACCESS_TOKEN=' }) -replace '^UPSTOX_SANDBOX_ACCESS_TOKEN=','')
python -m app sandbox-test
```

Step 5 is required **because of the §3 defect** — it puts the token in the
process environment so line 518 can see it.

**Do not pass `--place-order`.** It is `action="store_true"`, default off, and
`submit()` sits behind `if summary["credential_accepted"] and args.place_order:`.
Without the flag the command issues exactly one read-only
`GET /order/history?order_id=0`. No write path is reachable.

Steps 1–4 are entirely offline. Only step 5 touches the network.

---

## 6. Interpreting the probe

From `classify_history_probe` (`app/brokers/sandbox.py:134`):

| Response | Meaning |
|---|---|
| `400`/`404` carrying `UDAPI100010` | **Credential accepted** — probe order id does not exist. This is success. |
| `401` / `403` | Rejected. The broker then latches `available=False` for the object's lifetime, so re-runs repeat the same error. |
| `429` / `5xx` / transport error | **Not** a credential verdict. Do not read as a bad token. |

Treat the JSON `performed` field as authoritative rather than the exit code.

Two cautions:

- `UDAPI100050` appears **nowhere** in this codebase. The codes handled are
  `UDAPI100010`, `UDAPI100040`, `UDAPI1052`, `UDAPI10999`, `UDAPI11002`,
  `UDAPI11006`, `UDAPI1154`, `UDAPI99999`. If you saw that code it came from
  Upstox, not from AXIOM logic.
- Per §7.3 of `FULL_TROUBLESHOOTING_GUIDE.md`, a 401 does not by itself prove
  an invalid token — clock skew or a wrong host produce the same status.

---

## 7. Related findings

- **`upstox-test` does not test this credential.** It validates
  `settings.market_data_access_token` (`app/__main__.py:84`) and opens a
  WebSocket. It is the wrong tool for the sandbox order token, and per your
  instruction it was not run.
- **`sandbox-test` is the correct tool** — it is the repo's own read-only probe
  of the sandbox order credential.
- The earlier PC freeze was **not diagnosed**. `upstox-test` enters
  `asyncio.run` → `verify_feed_credential` (20 s default) → a WebSocket connect
  waiting `--timeout` (15 s default). That is a plausible contributor but I
  could not reproduce it and am not claiming it as the cause.

---

## 8. Hygiene

During testing a `.env` containing only dummy values was created and then
deleted. Verified afterwards:

```
.env present          : ABSENT
git log --all -- .env : (empty — never committed)
git status --porcelain: (clean)
HEAD / remote         : a921b8e / a921b8e
```

`.gitignore:1` is `.env`, confirmed by `git check-ignore -v`.

### Not done

- No source modification
- No `.env` committed
- No secret printed, requested, or logged
- No order, cancel, or modify issued
- No WebSocket feed or `upstox-test` executed
- No live Upstox request of any kind

---

## 9. Next action

Run steps 1–4 (offline). If step 4 reports `present: True`, run step 5 and
share the JSON. If it reports `present: False`, the token is not reaching
AXIOM's loader and no network probe is warranted — check the variable name
spelling and whether a machine-level variable is shadowing the file.
