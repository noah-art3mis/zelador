# Rust spike – read-only path (issue #47, step 1)

A partial Rust port of `zel`, built to answer whether a full port is worth it. It is not a replacement and is not installed anywhere. The Python CLI is still the product.

## What is ported

| Command                                    | Python source                                   |
|--------------------------------------------|-------------------------------------------------|
| `zel status [--json]`                      | `cli.status`, `status.py`, read half of `backup.py` and `write/changelog.py` |
| `zel audit tags\|collections [--since N] [--json]` | `audit/runner.py`, `report.py`, `tagmess.py`, `hygiene.py` |
| API reads (items, collections, tags, library version) | `client.py`: pagination and probe, Backoff/Retry-After, 429/503 retries, key redaction |
| Credentials, data dir, `config.yaml`        | `config.py`                                     |

Other audit checks and every other command are not ported. `zel audit` with no check, or with an unported one, exits 2 and says so.

## How it is checked

`tests/test_rust_differential.py` runs both CLIs against one `FakeZotero` and identical data directories. It covers 9 commands × 5 scenarios (normal, a forced pagination probe, a 429 retry, a 403 whose body leaks the key, a Backoff header). It asserts equal exit codes, stdout, stderr, files written and API request sequences. The Rust binary reaches the fake through a localhost bridge, using `ZELADOR_API_BASE`, a Rust-only test seam.

The suite catches every one of these planted bugs: lowercase instead of casefold, a removed pagination probe, compact JSON separators, no key redaction.

```bash
uv run pytest -q tests/test_rust_differential.py   # builds rust/ with cargo first; skips without cargo
cd rust && cargo test && cargo clippy -- -D warnings
```

Against a real library of several thousand items, `status --json` and `audit collections` matched byte for byte. The one difference was the repo-root finding below.

## Findings for the go/no-go decision

1. **Compatible output is a layer of its own.** Agents read `--json` byte for byte, and Rust's defaults differ from Python's: `serde_json` writes `{"a":1}`, Python writes `{"a": 1}`. `src/pyjson.rs` reproduces `json.dumps` (compact and `indent=2`), `repr()`, f-string `str()` and `:g`. `casefold()` comes from `caseless`, `isprintable()` from `unicode-general-category`, and `isoformat()` drops a zero fraction. Every further module will hit more of these. Changing the output contract once, deliberately, would be cheaper than mimicking it forever.
2. **Config location must be decided first.** Python finds `.env`, `config.yaml` and `taxonomy.yaml` next to its source. The binary uses the checkout it was built from, which a distributed binary would not have. The live run showed this: the binary built in a worktree reported `taxonomy_yaml: false` while Python reported `true`. A full port needs a platform config dir, and Python should move to it too.
3. **The library mapping in #47 is partly stale.** `serde_yaml` is deprecated on crates.io, so this uses `serde_yaml_ng`. `reqwest` 0.13 puts `RequestBuilder::query` behind a `query` feature, and its default TLS is now rustls, with no system OpenSSL.
4. **No speed case.** On a real library, `audit collections` spent well under 1% of its wall time on Python CPU; the cost is Zotero rendering bibliographies. A faster Rust run that came second probably hit a warm server cache, so it is not a fair comparison.
5. **Known unpinned differences.** These are transport-error wording (httpx vs reqwest), Unicode table versions (Python 3.13 vs the crates) for rare characters, and Windows data-dir parity, which is coded to platformdirs' rule but untested.

Still open from #47: a verdict on PDF crate coverage (`compress*.py`, `lookup/fulltext.py`) and the recorded go/no-go decision.
