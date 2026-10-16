# Kronos Real-Weights Verification

> **Historical model-artifact report:** this record describes its named baseline commit. Its important limitation remains: a local checksum/manifest is not publisher authenticity, and real pretrained inference must not be claimed without independent evidence. See [`AXIOM_CURRENT_AUDIT_REPORT.md`](AXIOM_CURRENT_AUDIT_REPORT.md) for current state.


**Date:** 2026-10-01
**Branch:** `arena/01a0ecfa-axiom`
**Base commit:** `21b81e2`
**Scope:** make "the code path works" impossible to confuse with "genuine
pretrained weights produced a forecast", and give AXIOM a deterministic way to
load real Kronos artifacts offline.

---

## Headline

**Genuine pretrained Kronos inference is NOT verified.** No real weights were
ever loaded in this environment. What is now true is that the system can no
longer *say* otherwise: every status is derived from evidence, and a model that
merely instantiates is structurally incapable of reaching
`REAL_INFERENCE_VERIFIED`.

The gate was not left as an assertion. It was attacked: a model built from tiny
**random** weights successfully loaded, ran real upstream inference, and
returned 8 finite steps — and was still refused, because 33,664 parameters is
not 24,700,000.

---

## A. VERIFIED

Everything below was executed in this session and produced the stated result.

| # | Claim | Evidence |
|---|---|---|
| A1 | The three official models and their tokenizer pairings are encoded as a hard table | `KNOWN_MODEL_PAIRINGS`: mini→`Kronos-Tokenizer-2k`, small→`Kronos-Tokenizer-base`, base→`Kronos-Tokenizer-base` |
| A2 | mini paired with the *base* tokenizer is rejected, not defaulted | `pairing_problems(mini, Tokenizer-base)` returns a non-empty reason |
| A3 | An unrecognised model is never silently paired | `expected_tokenizer("NeoQuasar/Kronos-large")` is `None`; `pairing_problems` reports it |
| A4 | All six required status states exist | `CODE_AVAILABLE`, `WEIGHTS_UNAVAILABLE`, `WEIGHTS_AVAILABLE`, `MODEL_LOADED`, `REAL_INFERENCE_VERIFIED`, `INFERENCE_FAILED` (plus `CODE_UNAVAILABLE`, `WEIGHTS_SUSPECT`) |
| A5 | Removing **any single** gate blocks verification | parametrised over all six gates — none reaches `REAL_INFERENCE_VERIFIED` |
| A6 | A model that loads but never infers reports `MODEL_LOADED`, not verified | `classify(loaded=True, inference_completed=False)` → `MODEL_LOADED` |
| A7 | **Random weights are detected and rejected** | measured 33,664 params vs published 24,700,000 (733×); `params_genuine=False` |
| A8 | Inference genuinely ran, and was still refused | `inference_result = "1 path(s) x 8 steps in 33.6 ms (structurally valid)"` with `real_inference_verified=False` |
| A9 | A NaN or Inf output is never silently accepted | `check_forecast_frame` reports `non-finite` |
| A10 | A `pred_len` mismatch is reported | 4-row frame against an 8-step request is flagged |
| A11 | Missing OHLCV columns are reported | dropping `close` is flagged |
| A12 | A wrong checksum blocks verification | mismatched digest → `checksum_ok=False` + problem recorded |
| A13 | Inspection alone can never claim a load | perfect artifacts still cap at `WEIGHTS_AVAILABLE`, `param_count == 0` |
| A14 | Load errors are surfaced, never swallowed | `RuntimeError` text appears in `problems` and `load_result` |
| A15 | The offline artifact directory is the one the loader reads | `AXIOM_KRONOS_ARTIFACTS` → `AXIOM_MODEL_CACHE` → platform default, matching `bootstrap.py:91` |
| A16 | Hugging Face cache layout is found | `models--NeoQuasar--Kronos-small/snapshots/<rev>/model.safetensors` is located |
| A17 | The smoke fixture is deterministic | two calls produce identical bars and timestamps; fixed origin `2024-01-02T09:15:00+00:00` |
| A18 | The gates are satisfiable, not vacuously false | a clean frame + genuine size + good checksum reaches `REAL_INFERENCE_VERIFIED` (positive control) |
| A19 | The report CLI refuses to exit 0 without verification | `verify_kronos_artifacts.py` exits 1 and prints `NOT VERIFIED` |
| A20 | **19/19 mutants killed** | every safety gate in `artifacts.py` is covered by a failing test |
| A21 | Baseline was green *before* mutation | 964 passed / 8 skipped with source; 946 / 26 without |
| A22 | CPU inference path works | device resolves to `cpu`; inference completes |
| A23 | No existing Kronos production code was modified | `backends.py`, `runtime.py`, `models.py`, `model_cache.py`, `forecast/` all show an empty diff |
| A24 | No weights are committed to Git | `.gitignore` already excludes `*.safetensors`, `*.bin`, `models/` |
| A25 | No secrets touched | no HF/broker token or API key is read, written or logged |
| A26 | No live Upstox calls | nothing in this change imports a broker or market provider |

---

## B. UNVERIFIED

Not tested here, and **not** claimed as working.

| # | Item | Why it is unverified |
|---|---|---|
| B1 | Inference with genuine pretrained weights | no real weights exist in this environment |
| B2 | `Kronos-mini` and `Kronos-base` loading | only their metadata is encoded; neither was loaded |
| B3 | Published parameter counts as exact values | taken from the upstream README as approximations (`~4.1M`, `~24.7M`, `~102.3M`); the check uses a 50% floor, not equality |
| B4 | Tokenizer parameter counts | no published figures exist, so no size gate is applied to the tokenizer |
| B5 | A full-size randomly-initialised model | **cannot** be detected by parameter count — only the checksum gate catches this |
| B6 | Correctness of forecast *values* | nothing here judges whether a prediction is good, only that it is structurally valid |
| B7 | The `TestArtifactSmoke` class | it has never executed; it skips by design |
| B8 | Multi-GPU / mixed-precision behaviour | untouched |

---

## C. BLOCKED BY ENVIRONMENT

| # | Blocker | Detail |
|---|---|---|
| C1 | **Hugging Face is unreachable from this sandbox** | the single root cause. `from_pretrained` cannot fetch anything, so no genuine artifact can exist here. Not retried — the constraint was explicit. |
| C2 | **CUDA cannot be claimed** | no GPU is present. CUDA support is unchanged and still unverified. |
| C3 | Sandbox resets wipe `site-packages`, `/tmp`, and external clones | the Kronos checkout had to be re-cloned this session; expect it again |
| C4 | PySide6 widgets cannot render | `libGL.so.1` absent; irrelevant to this change but still true |

---

## D. FILES CHANGED

**New**

| File | Lines | Purpose |
|---|---|---|
| `axiom/kronos/artifacts.py` | 598 | status enum, pairing/param tables, the `classify` gate, `ArtifactReport`, `inspect_artifact`, `verify_model`, deterministic fixture, output checks |
| `tests/test_kronos_artifacts.py` | 763 | 70 tests across 10 classes, including the gated smoke test |
| `tools/verify_kronos_artifacts.py` | 173 | human + JSON verification report; exits non-zero unless verified |
| `tools/mutate_kronos_artifacts.py` | 141 | reproducible mutation harness with a baseline-green gate |

**Modified**

| File | Change |
|---|---|
| `axiom/kronos/__init__.py` | export wiring only — added the 8 new public names. As a side effect `ruff --fix` repaired one **pre-existing** `RUF022` (`__all__` unsorted), taking the repo from 720 to 719 lint errors. |

**Deliberately untouched:** `axiom/kronos/backends.py`, `axiom/kronos/runtime.py`,
`axiom/kronos/model_cache.py`, `axiom/models.py`, `axiom/forecast/`. No bug was
found in the existing integration, so per the brief it was left alone. The new
module *reuses* `ModelCache` rather than duplicating its manifest and SHA-256
machinery.

---

## E. TESTS RUN

| Configuration | Result |
|---|---|
| `AXIOM_KRONOS_SOURCE` set (real upstream classes) | **964 passed, 8 skipped** |
| `requirements.txt` only | **946 passed, 26 skipped** |
| `tests/test_kronos_artifacts.py` alone | **69 passed, 6 skipped** |
| Mutation sweep | **19/19 killed**, baseline confirmed green first |
| `ruff check` on the four touched files | clean |

968 tests collected in total. The 6 skips in the new file are the
`TestArtifactSmoke` class refusing to run without genuine weights — that is the
intended behaviour, not a gap.

The mutation sweep is the load-bearing evidence. Its first run killed **18/19**;
the survivor was `verify_model` marking inference complete regardless of output
validity — a genuine hole, since no test then proved a bad frame blocks
verification. Four tests were added (`TestVerifyModelRejectsInvalidOutput`),
including a positive control proving the gates are satisfiable. The re-run
killed 19/19.

---

## F. EXACT COMMANDS USED

```bash
# environment recovery after a sandbox reset
git fetch origin arena/01a0ecfa-axiom && git reset -q FETCH_HEAD
git clone --depth 1 https://github.com/shiyu-coder/Kronos /home/user/kronos-upstream

# measure the parameter-count discriminator
AXIOM_KRONOS_SOURCE=/home/user/kronos-upstream python3 -c "<count params of the toy config>"

# baseline BEFORE mutation (mandatory order)
AXIOM_KRONOS_SOURCE=/home/user/kronos-upstream python3 -m pytest -q -p no:cacheprovider
python3 -m pytest -q -p no:cacheprovider

# the new tests
AXIOM_KRONOS_SOURCE=/home/user/kronos-upstream \
  python3 -m pytest tests/test_kronos_artifacts.py -q -p no:cacheprovider -rs

# mutation, only after the baseline was green
AXIOM_KRONOS_SOURCE=/home/user/kronos-upstream python3 tools/mutate_kronos_artifacts.py

# the report
python3 tools/verify_kronos_artifacts.py
python3 tools/verify_kronos_artifacts.py --load --json results/kronos_artifacts.json

# lint
ruff check axiom/kronos/artifacts.py tests/test_kronos_artifacts.py \
           tools/verify_kronos_artifacts.py tools/mutate_kronos_artifacts.py
```

---

## G. ARTIFACT REQUIREMENTS

What must exist before `REAL_INFERENCE_VERIFIED` can ever be reported.

**1. Genuine weight files** for each model you intend to use, in Hugging Face
cache layout under one directory:

```
<artifact-dir>/models--NeoQuasar--Kronos-small/snapshots/<rev>/model.safetensors
<artifact-dir>/models--NeoQuasar--Kronos-Tokenizer-base/snapshots/<rev>/model.safetensors
```

**2. The directory pointed at by an environment variable**, either of which
works — they resolve to the same place the loader already reads:

```bash
export AXIOM_KRONOS_ARTIFACTS=/path/to/kronos-artifacts
# or, equivalently:
export AXIOM_MODEL_CACHE=/path/to/kronos-artifacts
```

**3. A recorded SHA-256** for each artifact. This is not optional in spirit: it
is the *only* defence against a full-size randomly-initialised model, which has
the correct parameter count and is otherwise indistinguishable. Pass it to
`verify_model(..., expected_checksum=...)` or record it in the `ModelCache`
manifest.

**4. Correct tokenizer pairing.** mini must ship with `Kronos-Tokenizer-2k`;
small and base with `Kronos-Tokenizer-base`. A mismatch is reported, never
repaired.

**5. Roughly the published parameter count.** At least 50% of
`~4.1M` / `~24.7M` / `~102.3M`. This is the gate that rejects random weights.

Nothing here downloads. Weights stay out of Git via the existing `.gitignore`.

---

## H. NEXT HUMAN ACTION REQUIRED

**One blocker, one action.**

On the development PC — which *can* reach Hugging Face:

```bash
# 1. obtain genuine weights
pip install "huggingface_hub[cli]"
huggingface-cli download NeoQuasar/Kronos-small
huggingface-cli download NeoQuasar/Kronos-Tokenizer-base

# 2. record the checksums while the source is trusted
sha256sum ~/.cache/huggingface/hub/models--NeoQuasar--Kronos-small/snapshots/*/*.safetensors

# 3. confirm it verifies on that machine
export AXIOM_KRONOS_SOURCE=/path/to/Kronos
export AXIOM_KRONOS_ARTIFACTS=~/.cache/huggingface/hub
python3 tools/verify_kronos_artifacts.py --load --json results/kronos_artifacts.json
```

That command exits 0 **only** if genuine weights loaded and real inference ran.
It also un-skips `TestArtifactSmoke`, which then runs on the fixed OHLCV
fixture and writes a machine-readable result.

**Then, and only then**, copy the artifact directory to any machine that must
run offline and repeat step 3 there.

Until that happens, every Kronos forecast in AXIOM is produced by
non-pretrained weights and must be labelled `WEIGHTS_UNAVAILABLE`. The code now
enforces that label rather than trusting anyone to apply it.
