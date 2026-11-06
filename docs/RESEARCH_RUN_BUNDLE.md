# Portable research-run bundles

A research run inside AXIOM produces a manifest, a split definition, a benchmark,
a cost model, corporate-action provenance, a software fingerprint and a
qualification record. All of it lives in one local SQLite store, on one machine,
pointed at one dataset directory. That is enough to audit the work *here*. It is
not enough to hand the work to somebody else, to verify it years later, or to
prove that the numbers in a screenshot came from the inputs that are still on
disk.

`app/model_lab/bundle.py` closes that gap. It exports one completed run as a
**portable research-run bundle**: a self-contained, content-addressed evidence
package that can be verified with no network access and replayed from its own
pinned inputs.

> A bundle is evidence, not authority. It never re-scores a run upward, never
> arms a strategy, never selects a paper candidate, never sizes a position,
> never issues an `ExecutionPermit`, never places an order and never enables
> live trading. Nothing in this document claims that a run, a bundle or a
> replay is profitable.

## Two different things called "bundle"

| Name | Schema | What it is |
| --- | --- | --- |
| Model bundle (Model Registry, Panel 8) | `axiom.model_lab.bundle.v1` | Packaged **model weights/params** for export and re-import |
| Research-run bundle (this document) | `axiom.research.bundle.v1` | Packaged **research evidence** for one completed run |

They share a word and nothing else. A research-run bundle contains no model
weights and cannot be imported into the registry.

## What a bundle pins

| Pinned item | Artifact role | Verification |
| --- | --- | --- |
| Canonical run manifest | `run_manifest` | re-hashes to its own `manifest_sha256`; every section re-checked |
| Raw dataset identity (id, sha256, symbol, market, timeframe, range, costs basis) | `dataset_meta` | must agree with the manifest's `dataset` block |
| Raw dataset bars | `dataset_bars` | `canonical_bars_sha256(bars)` must equal the pinned digest |
| Corporate-action sidecar + hash | `corporate_actions` (optional) | its SHA-256 must equal the manifest's `corporate_actions.actions_sha256` |
| Strategy / config (`run_config`) | `run_config` | must agree with the manifest's search family and cost block |
| Feature-schema fingerprint | `feature_schema` | `FEATURE_NAMES` digest must equal the manifest's `feature_set_sha256` |
| Dependency-lock identity | `dependency_lock` | pinned by content hash; `DEPENDENCY_LOCK_CANDIDATES` resolved at export |
| Software commit + cleanliness | `software_fingerprint` | must equal the manifest's `software` block |
| Provider-conformance evidence | `conformance_evidence` | the same offline suite report the qualification used |
| Qualification inputs | `qualification_inputs` | provenance attestation, expected commit, recomputed dataset digest, conformance digest |
| Qualification record | `qualification_record` | the record must hash to itself **and** re-derive from the bundle |
| Benchmark, universe, folds, variants, costs, run outputs | `sections` in the index | each section carries the manifest's own section digest |

Sections that are pins rather than files (`sections.search_family`,
`folds`, `benchmark`, `universe`, `costs`, `strategy_versions`,
`dependency_lock`, `run_outputs`) are compared by digest against the manifest,
so a bundle cannot quietly re-describe the run it claims to contain.

## Format

```
axiom.research.bundle.v1        bundle.schema
1                               bundle.format_version   (SUPPORTED_FORMAT_VERSIONS = {1})
```

One bundle is one directory, one `.zip`, or one sealed `.json` document, each
holding a single index and the payloads it enumerates:

```
bundle.json                     the index (dir/zip)      |  the whole document (sealed JSON)
artifacts/<sha256>.json         content-addressed payloads
artifacts/<sha256>.txt          text payloads (the dependency lock)
```

The sealed form inlines every payload into `inline` keyed by path:

```json
{
  "schema": "axiom.research.bundle.v1",
  "format_version": 1,
  "bundle_sha256": "<sha256 of the index without bundle_sha256/created_at/generator/inline>",
  "created_at": "2026-11-07T09:12:44.120075+00:00",
  "generator": {"name": "axiom-model-lab", "python": "3.11.9", "platform": "Linux-…"},
  "limits": {"max_artifacts": 64, "max_artifact_bytes": 33554432,
             "max_total_bytes": 100663296, "max_index_bytes": 4194304},
  "run": {"run_id": "mlrun-…", "market": "NSE", "timeframe": "1h",
           "dataset_id": "…", "manifest_sha256": "…",
           "qualification_status": "BLOCKED", "qualification_record_sha256": "…",
           "candidate_id": "…"},
  "artifacts": [
    {"role": "run_manifest", "path": "artifacts/<sha256>.json", "sha256": "…",
     "size": 15412, "media_type": "application/json", "encoding": "utf-8",
     "required": true}
  ],
  "sections": {"dataset": {…}, "strategy_versions": {…}, "run_config": {…},
               "feature_schema": {…}, "search_family": {…}, "folds": {…},
               "benchmark": {…}, "universe": {…}, "costs": {…},
               "software": {…}, "dependency_lock": {…},
               "corporate_actions": {…}|null, "run_outputs": {…}},
  "qualification": {"status": "BLOCKED", "record_sha256": "…",
                    "verification_level": "OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED",
                    "blocked": ["SYNTHETIC_DATA", "STALE_DATA"], "unverified": [],
                    "execution_authority": "NONE",
                    "upgrade_effect": "NONE — a bundle never re-scores, upgrades or authorizes a qualification"},
  "execution_view": {"authority": "NONE", "live_trading": false,
                     "broker_order_api_calls": 0, "armed": false},
  "content_policy": {"credentials_included": false, "user_data_dirs_included": false,
                     "network_calls": 0, "broker_order_api_calls": 0, "live_trading": false},
  "integrity_scope": "…hashes prove the bytes, not the provider…",
  "inline": {"artifacts/<sha256>.json": {"encoding": "utf-8", "content": "…"}}
}
```

Determinism: two exports of the same run produce the same `bundle_sha256`,
the same artifact list (roles, digests, paths, sizes) and the same sections.
`created_at`, `generator` and `inline` are excluded from `bundle_sha256`; every
other field is covered.

### Content addressing

An artifact path is `artifacts/<sha256>.<ext>` where `<sha256>` is the SHA-256
of the file's exact bytes and `<ext>` is `json` for `application/json` or `txt`
for `text/plain`. The path, the index entry and the file content must agree;
three separate checks enforce that (`ARTIFACT_PATH_HASH_MISMATCH`,
`ARTIFACT_PATH_NOT_CONTENT_ADDRESSED`, `ARTIFACT_HASH_MISMATCH`).

### Limits

| Limit | Default | Enforced on |
| --- | --- | --- |
| `max_artifacts` | 64 | export and verify |
| `max_artifact_bytes` | 32 MiB | export and verify |
| `max_total_bytes` | 96 MiB | export and verify |
| `max_index_bytes` | 4 MiB | verify |

`BundleLimits` can be passed to `verify_bundle(..., limits=...)`; the API route
additionally rejects oversized request bodies with HTTP 413.

## Verification (offline)

`verify_bundle(path_or_document)` reads the container, never the network, and
returns a report:

```json
{"schema": "axiom.research.bundle.verify.v1", "status": "PASS" | "FAIL",
 "container": "DIRECTORY" | "ZIP" | "SEALED_JSON",
 "bundle_sha256": "…", "manifest_sha256": "…", "bundle_schema": "…",
 "checked": {"artifacts": 10, "bytes": 412331},
 "qualification": {"bundled_status": "BLOCKED", "rederived_status": "BLOCKED",
                   "matches": true, "record_content_digest": "…", …},
 "reason_codes": [], "findings": [], "execution_authority": "NONE",
 "network_calls": 0, "broker_order_api_calls": 0, "live_trading": false}
```

`PASS` requires **every** check to succeed. A single finding fails the bundle —
there is no partial pass, and there is no way to "accept anyway" flag.

Order of checks:

1. container and index: presence, JSON validity, schema, `format_version`,
   `bundle_sha256` over the index.
2. artifact index: required roles present, unique roles, unique paths, paths
   safe and content-addressed, files present, regular files (no symlinks), no
   unreferenced files, size and hash match, limits.
3. semantic consistency: manifest digest, bars hash, dataset meta, corporate
   action hash, section digests, software fingerprint, dependency lock, run
   config, bars parseable.
4. qualification: record self-digest, execution-authority scope, full
   re-derivation with `evaluate_qualification`, index-vs-record agreement.
5. secrets: every payload scanned for credential-shaped text.

### Reason codes

Verification and replay share one stable table (`REASON_CODES` in
`app/model_lab/bundle.py`, served at `GET /api/model-lab/bundles/format`).

**Container / index**

| Code | Meaning |
| --- | --- |
| `BUNDLE_NOT_FOUND` | the path does not exist |
| `BUNDLE_MANIFEST_MISSING` | `bundle.json` is absent |
| `BUNDLE_MANIFEST_UNREADABLE` | the index is not valid JSON |
| `BUNDLE_MANIFEST_INVALID` | the index is missing required fields |
| `UNSUPPORTED_BUNDLE_SCHEMA` | not an `axiom.research.bundle.v1` document |
| `UNSUPPORTED_BUNDLE_VERSION` | `format_version` is newer than this build supports |
| `BUNDLE_DIGEST_MISMATCH` | `bundle_sha256` does not match the recomputed index digest |

**Artifact index**

| Code | Meaning |
| --- | --- |
| `ARTIFACT_ROLE_MISSING` | a required role is absent from the index |
| `ARTIFACT_NOT_IN_BUNDLE` | the index enumerates a file the container does not hold |
| `ARTIFACT_UNREFERENCED` | the container holds a file the index does not enumerate |
| `ARTIFACT_HASH_MISMATCH` | an artifact's bytes do not hash to the enumerated SHA-256 |
| `ARTIFACT_SIZE_MISMATCH` | an artifact's length differs from the enumerated size |
| `ARTIFACT_DUPLICATE_PATH` | two entries share a path |
| `ARTIFACT_DUPLICATE_ROLE` | two entries share a role |
| `ARTIFACT_PATH_UNSAFE` | an absolute, traversing, non-normalised or percent-encoded path |
| `ARTIFACT_PATH_NOT_CONTENT_ADDRESSED` | a path that is not `artifacts/<sha256>.<ext>` |
| `ARTIFACT_PATH_HASH_MISMATCH` | the hash in the path differs from the index entry |
| `ARTIFACT_SYMLINK` | an artifact is a symlink instead of a regular file |
| `ARTIFACT_OVERSIZED` | an artifact exceeds `max_artifact_bytes` |
| `ARTIFACTS_TOO_MANY` | more artifacts than `max_artifacts` |
| `BUNDLE_TOO_LARGE` | total payload exceeds `max_total_bytes` |

**Semantic consistency**

| Code | Meaning |
| --- | --- |
| `MANIFEST_TAMPERED` | the manifest does not match its own digest |
| `DATASET_HASH_MISMATCH` | the bundled bars do not re-hash to the pinned dataset digest |
| `DATASET_META_MISMATCH` | dataset metadata contradicts the manifest |
| `ACTION_HASH_MISMATCH` | the corporate-action sidecar does not match its pinned hash |
| `SECTION_DIGEST_MISMATCH` | a pinned section does not match the manifest |
| `SOFTWARE_FINGERPRINT_MISMATCH` | the software artifact contradicts the manifest |
| `DEPENDENCY_LOCK_MISSING` / `DEPENDENCY_LOCK_MISMATCH` | no lock pinned / lock contradicts its pin |
| `RUN_CONFIG_MISMATCH` | the run configuration contradicts the manifest |
| `BARS_UNREADABLE` | the bars are not a parseable bar series |

**Qualification**

| Code | Meaning |
| --- | --- |
| `QUALIFICATION_RECORD_MISSING` | no record in the bundle |
| `QUALIFICATION_ALTERED` | the record does not hash to itself, or does not re-derive from the bundle |
| `INDEX_RUN_MISMATCH` | the index `run`/`qualification` block contradicts the manifest or record |
| `QUALIFICATION_SCOPE_VIOLATION` | the record claims execution authority |
| `QUALIFICATION_INPUTS_MISSING` | the qualification inputs are not pinned |
| `CONFORMANCE_EVIDENCE_MISSING` | provider-conformance evidence is not pinned |

**Secrets and replay**

| Code | Meaning |
| --- | --- |
| `SECRET_DETECTED` | a payload contains credential-shaped text (export aborts) |
| `REPLAY_MANIFEST_MISMATCH` | the replayed manifest digest differs from the pinned one |
| `REPLAY_METRIC_DRIFT` | a replayed metric differs beyond tolerance |
| `REPLAY_NOT_REPRODUCIBLE` | the bundle does not verify, or inputs are insufficient |
| `REPLAY_REQUIRES_KRONOS` | the run evaluated genuine Kronos, which replay cannot reproduce offline |

Reason codes are stable once published; new checks add new codes rather than
reusing existing ones.

### Attack models the verifier rejects

| Attack | Caught by |
| --- | --- |
| Edit a payload, leave the index | `ARTIFACT_HASH_MISMATCH` |
| Edit a payload **and** re-sign every hash, path and the bundle digest | the semantic checks of step 3 and 4 |
| Forge `QUALIFIED` in the record without re-signing it | `QUALIFICATION_ALTERED` (self-digest) |
| Forge `QUALIFIED` *and* recompute the record digest and re-seal everything | `QUALIFICATION_ALTERED` (re-derivation) + `INDEX_RUN_MISMATCH` |
| Claim execution authority in the record | `QUALIFICATION_SCOPE_VIOLATION` |
| Swap the dataset for a friendlier series | `DATASET_HASH_MISMATCH` |
| Re-point a path at `../../etc/passwd`, `%2f`, `..\\`, or a symlink | `ARTIFACT_PATH_UNSAFE`, `ARTIFACT_PATH_NOT_CONTENT_ADDRESSED`, `ARTIFACT_SYMLINK` |
| Drop an artifact, add a duplicate, or smuggle an extra file | `ARTIFACT_ROLE_MISSING`, `ARTIFACT_DUPLICATE_*`, `ARTIFACT_UNREFERENCED` |
| Declare a newer format to change a rule's meaning | `UNSUPPORTED_BUNDLE_VERSION` |

## Replay

`replay_bundle(path_or_document, abs_tol=1e-9, rel_tol=1e-9, strict_environment=False)`
verifies first, then re-runs `execute_research_pipeline` **in-process** using
only the bundle's own artifacts: `dataset_meta`, `dataset_bars`, `run_config`,
the pinned software block, and the qualification re-derivation. It reads no
dataset directory, contacts no provider, submits no order.

| Replay status | Meaning |
| --- | --- |
| `REPRODUCED` | identical manifest digest and identical qualification record, no environment difference |
| `REPRODUCED_WITH_ENVIRONMENT_DRIFT` | reproduced exactly, but the machine differs from the pinned one (commit, cleanliness, Python, platform, package version) |
| `DIVERGED` | the replayed manifest digest differs, a metric moved beyond tolerance, or environment drift occurred with `strict_environment=True` |
| `UNVERIFIED` | the bundle did not verify, inputs are insufficient, or the run needs Kronos |

Tolerance: the pipeline is deterministic, so the default is `abs = rel = 1e-9`;
the tolerance exists only to absorb float formatting differences across platforms
and Python builds. Both can be set explicitly (CLI `--abs-tol/--rel-tol`, API
`abs_tol`/`rel_tol`, or `replay_bundle(...)`), and the value used is reported in
`tolerance`. Metrics compared: candidate metrics and rankings, the search family
and its correction, the split boundaries, and the benchmark series.

Environment drift is reported field by field (`commit`, `tree_dirty`, `python`,
`platform`, `package_version`, `commit_source`, `frozen`). A dirty worktree at
replay time is drift, not divergence: the pinned commit is still reported, and
the replay result is labelled `REPRODUCED_WITH_ENVIRONMENT_DRIFT`. Nothing about
the original evidence changes.

**Kronos.** If the pinned run evaluated genuine Kronos, the bundle does not
contain the Kronos weights or runtime, so replay returns `UNVERIFIED` with
`REPLAY_REQUIRES_KRONOS`. AXIOM never substitutes a mock forecast to make a
Kronos run look reproducible.

**Qualification invariance.** The replay report carries
`qualification.effective_status = worse_of(bundled, re-derived)`, a boolean
`upgraded` (always `false`), and `execution_authority: "NONE"`. A successful
replay of a `BLOCKED` run is still `BLOCKED`; a successful replay of an
`UNVERIFIED` run is still `UNVERIFIED`. Synthetic data stays ineligible:
`bundled synthetic fixture ⇒ BLOCKED (SYNTHETIC_DATA, STALE_DATA)` on export,
on verify and after replay.

## Safety and scope

* **No credentials.** Export scans every payload for private keys, bearer
  tokens, `"key": "value"` credential pairs, `key=value` credential pairs and
  long opaque tokens, and aborts with `SECRET_DETECTED` (masked in the message)
  rather than writing the file. Bundles never include user-data directories,
  unrelated files, the SQLite store, `sources/`, or any path outside the run's
  own artifacts. The nine artifacts are constructed from named inputs — there is
  no directory walk — so an unrelated local file cannot be swept in.
* **No network, no broker.** Export reads the local store, the dataset directory
  and the dependency lock. Verify reads bundle bytes. Replay runs the research
  pipeline in-process. None imports a broker adapter or an HTTP client for
  trading; the tests run the whole cycle with `socket.connect` patched to raise.
  `broker_order_api_calls` and `network_calls` in every report are `0` by
  construction and asserted in tests.
* **No execution authority.** Export refuses to bundle a record that claims
  authority, verify rejects one, and the replay report never mints an
  `ExecutionPermit`. `live_trading` is `false` throughout.
* **Attestation stays attestation.** The bundle preserves
  `verification_level: OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED` and the
  record's `open_reviews`, including `INDEPENDENT_PROVENANCE_REVIEW`. A hash
  matching proves the bytes are the bundled bytes. It does **not** prove the
  provider's data is genuine, that the capture was honest, or that the operator
  told the truth — those remain the record's own attestation level.

## Commands

```bash
# export a completed run (directory of content-addressed files)
python -m app research-bundle export --run mlrun-<id> --output ./bundle-out

# alternatives
python -m app research-bundle export --run mlrun-<id> --output ./bundle.zip  --archive
python -m app research-bundle export --run mlrun-<id> --output ./bundle.json --sealed

# verify (offline; exit 0 = PASS, 1 = FAIL)
python -m app research-bundle verify --bundle ./bundle-out
python -m app research-bundle verify --bundle ./bundle-out --json --output report.json

# replay from pinned inputs only
python -m app research-bundle replay --bundle ./bundle-out
python -m app research-bundle replay --bundle ./bundle-out --abs-tol 1e-9 --rel-tol 1e-9 --json

# read-only re-verify against a fresh pair of eyes
python -m app research-bundle export --run mlrun-<id> --store /path/to/model_lab
```

Exit codes: export `0` written / `2` refused (reason on stderr). Verify `0`
PASS / `1` FAIL. Replay `0` `REPRODUCED` or `REPRODUCED_WITH_ENVIRONMENT_DRIFT`
/ `1` `DIVERGED` / `3` `UNVERIFIED`.

HTTP (authenticated the same way as the rest of `/api`):

```bash
GET  /api/model-lab/bundles/format                     # schema, roles, limits, reason codes
POST /api/model-lab/runs/{run_id}/bundle               # -> sealed bundle document
POST /api/model-lab/bundles/verify   {"bundle": {...}} # -> verification report
POST /api/model-lab/bundles/replay   {"bundle": {...}, "abs_tol": 1e-9,
                                      "rel_tol": 1e-9, "strict_environment": false}
```

`409` means the run has no manifest or no qualification record yet; `404` means
the run does not exist; `413` means the request body exceeds the size limit.

## Compatibility rules

1. `bundle.schema` must be exactly `axiom.research.bundle.v1`.
2. `bundle.format_version` must be in `SUPPORTED_FORMAT_VERSIONS` (`{1}`).
   A newer version is rejected with `UNSUPPORTED_BUNDLE_VERSION` rather than
   interpreted with older rules.
3. Within a format version, fields may be **added** to the index and to
   findings; readers ignore unknown fields they were not asked about. Removing,
   renaming or re-meaning a field, and changing the digest scope of
   `bundle_sha256`, requires a new `format_version`.
4. Reason codes are additive. Once published, a code keeps its meaning; a new
   check introduces a new code.
5. Artifact paths stay `artifacts/<sha256>.<ext>` with the same two media types;
   a new media type is an additive change to the `_EXT_BY_MEDIA` mapping, and an
   unknown extension is rejected rather than guessed.
6. Old bundles stay verifiable: verification for a given `format_version` does
   not depend on the local store, the dataset directory, the clock, or the
   network. Only the bundled bytes and the current rule table matter.

## Limits and unverified work

* Hashing is integrity, not truth. A verified bundle shows the evidence is
  intact and internally consistent; it cannot detect a provider that shipped bad
  data, an operator who attested something false, or a corporate action that was
  recorded but is factually wrong.
* Replay reproduces the **research pipeline**, not the original database. It
  cannot reproduce a run whose inputs were edited after the fact (such a bundle
  simply fails verification).
* Kronos-evaluated runs cannot be replayed; they stay `UNVERIFIED`. There is no
  mock substitution and no plan to add one.
* Bundles are per run. There is no multi-run index, no bundle diffing and no
  bundle-level signing key: integrity comes from the digests inside the
  document, so an adversary who can rewrite every file can rewrite the bundle —
  what they cannot do is make a forged **qualification record** re-derive from a
  manifest and inputs that do not support it, provided the recipient obtains the
  bundle digest (`bundle_sha256`) through a channel they trust. Publishing that
  digest out of band (ticket, commit message, email) is the recommended
  practice.
* Native Windows packaging of the CLI commands is unverified; the commands are
  pure stdlib plus the existing app, and were exercised on Linux only.
* No live provider, real credential asset, broker account or exchange was
  contacted at any point. The synthetic fixture (`bundled_nse_synthetic_1h`)
  used in tests is ineligible for qualification by design.

## Tests

`tests/test_research_bundles.py` — 65 offline tests:

| Group | Covers |
| --- | --- |
| export | determinism, complete artifact table, pinned inputs, no secrets, no authority, refusal on credential-shaped payloads, tampered manifest, mismatched bars, missing dependency lock |
| containers | sealed JSON, directory, zip, round trip through disk |
| tampering | every required role, the optional corporate-action sidecar, manifest, bars, config, software, section pin, index run block, bundle digest |
| structure | missing role, missing file, duplicate role/path, unreferenced file |
| traversal | `..`, absolute, percent-encoded, backslash, zip-slip member, symlink, non-content-addressed path |
| versions | newer `format_version`, foreign schema, limits (artifact size, count, total), missing path, directory without index |
| qualification | synthetic stays `BLOCKED`, forged `QUALIFIED` (naive and fully re-signed), authority claim, attestation level preserved |
| replay | reproduced manifest, no upgrade, environment drift, verification gate, substituted bars, tolerance, Kronos → `UNVERIFIED` |
| isolation | zero sockets during export/verify/replay, no order surface in the module |
| interfaces | CLI round trip and failure exit code, API export/verify/replay and `404` |
