# Rust port (issue #47, SPEC.md milestone M6)

A partial Rust port of `zel`. Step 1 (issue #47) was a spike to decide whether a full port was worth it; the decision was a phased port, recorded as milestone M6 in `SPEC.md`. It is not installed anywhere yet. The Python CLI stays the product and the reference until cutover (R5).

## What is ported

| Command                                    | Python source                                   |
|--------------------------------------------|-------------------------------------------------|
| `zel backup [--json]`                      | `backup.py` (writer), `cli.backup` – the JSONL file is byte-identical to Python's |
| `zel status [--json]`                      | `cli.status`, `status.py`, read half of `backup.py` and `write/changelog.py` |
| `zel audit tags\|collections [--since N] [--json]` | `audit/runner.py`, `report.py`, `tagmess.py`, `hygiene.py` |
| API reads (items, collections, tags, library version) | `client.py`: pagination and probe, Backoff/Retry-After, 429/503 retries, key redaction |
| Credentials, data dir, `config.yaml`        | `config.py`                                     |

Other audit checks and every other command are not ported. `zel audit` with no check, or with an unported one, exits 2 and says so.

## How it is checked

`tests/test_rust_differential.py` runs both CLIs against one `FakeZotero` and identical data directories. Both run with the clock frozen at one instant (Python in-process, Rust through the debug-only `ZELADOR_NOW`), so timestamps and ids compare exactly. It covers 11 commands × 9 scenarios: normal, a forced pagination probe, a 429 retry, a 403 whose body leaks the key, a Backoff header, a 302 that must not be followed (run for `status` only), a library unchanged since the last backup, number text Python would not write (`0.00001`, `1E5`, `-0`), and a seeded `config.yaml` plus registry. Five more cases pin the default config and data dirs to platformdirs' XDG rule. It asserts equal exit codes, stdout, stderr, files written and API request sequences. The Rust binary reaches the fake through a localhost bridge, using `ZELADOR_API_BASE`, a Rust-only test seam that only debug builds honour.

The suite catches every one of these planted bugs: lowercase instead of casefold, a removed pagination probe, compact JSON separators, no key redaction.

```bash
uv run pytest -q tests/test_rust_differential.py   # builds rust/ with cargo first; skips without cargo
cd rust && cargo test && cargo clippy -- -D warnings
```

Against a real library of several thousand items, `status --json` and `audit collections` matched byte for byte apart from timestamps. The one difference was the repo-root finding below.

## Findings for the go/no-go decision

1. **Compatible output is a layer of its own.** Agents read `--json` byte for byte, and Rust's defaults differ from Python's: `serde_json` writes `{"a":1}`, Python writes `{"a": 1}`. Numbers too: serde_json keeps each number's source text (`arbitrary_precision`), and `py_number` re-renders it as Python would after `json.loads` – integers with every digit, `-0` as `0`, floats by `repr` (`1e-05`, `1e+16`, `100000.0`). `src/pyjson.rs` reproduces `json.dumps` (compact and `indent=2`), `repr()`, f-string `str()` and `:g`. `casefold()` comes from `caseless`, `isprintable()` from `unicode-general-category`, and `isoformat()` drops a zero fraction. Every further module will hit more of these. Changing the output contract once, deliberately, would be cheaper than mimicking it forever.
2. **Config location must be decided first** (resolved in M6 R1: both implementations now read the platform config dir, and the differential test seeds it). Python found `.env`, `config.yaml` and `taxonomy.yaml` next to its source. The binary used the checkout it was built from, which a distributed binary would not have. The live run showed this: the binary built in a worktree reported `taxonomy_yaml: false` while Python reported `true`. A full port needs a platform config dir, and Python should move to it too.
3. **The library mapping in #47 is partly stale.** `serde_yaml` is deprecated on crates.io, so this uses `serde_yaml_ng`. `reqwest` 0.13 puts `RequestBuilder::query` behind a `query` feature, and its default TLS is now rustls, with no system OpenSSL.
4. **No speed case.** On a real library, `audit collections` spent well under 1% of its wall time on Python CPU; the cost is Zotero rendering bibliographies. A faster Rust run that came second probably hit a warm server cache, so it is not a fair comparison.
5. **Known unpinned differences.** None of these can happen with well-formed data and a working setup:
   - `zel audit` does not load `taxonomy.yaml`, so a malformed file stops Python with exit 1 while Rust runs the check.
   - Malformed input: an HTTP-date `Retry-After`, a non-numeric `Backoff`, a response without `Last-Modified-Version`, log entries with missing or non-string fields, a non-UTF-8 file in `log/`, `meta.type` of `1.0` or `true`, lone JSON surrogates. Python raises or crashes; Rust tolerates the input or prints a clean error.
   - Config parsing: a malformed `.env` line stops dotenvy, so later variables never load, while python-dotenv warns and continues. YAML 1.1 booleans (`style: no`) parse as strings in `serde_yaml_ng` but as `False` in PyYAML.
   - JSON: a number that overflows a float (`1e400`) is written as `Infinity` by both, which Python's reader accepts and serde_json's refuses, so Rust cannot read back such a backup; a lone surrogate (`"\ud800"`) fails Rust's parse at the API response and Python's write later.
   - Formatting and platform: `fmt_g` matches Python's `:g` only for whole numbers and short fractions; transport-error wording differs (httpx vs reqwest); Unicode table versions differ for rare characters; on Windows, Python writes the locale encoding with `\r\n` and Rust UTF-8 with `\n`; Windows config-dir and data-dir parity is coded to platformdirs' rule but untested; non-UTF-8 XDG values, an empty `HOME`, ASCII separator characters (`\x1c`–`\x1f`) around XDG values, and a leading `//` resolve differently from platformdirs and pathlib; on Linux the default-dir rule (XDG values stripped, relative ones accepted) is pinned by the differential test.

Still open from #47: a verdict on PDF crate coverage (`compress*.py`, `lookup/fulltext.py`) and the recorded go/no-go decision.
