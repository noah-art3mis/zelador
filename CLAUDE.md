# zelador — agent guide

Deterministic CLI over the Zotero Web API; full design in `SPEC.md`. The agent proposes, deterministic code validates, the user approves, every applied change is logged and reversible. Run everything with `uv run zel ...`; add `--json` when you are the consumer (NDJSON, one object per line). Diagnostics arrive on stderr; exit 0 success, 1 operational failure, 2 bad input.

## Situation → command

- Starting any session → `uv run zel status --json` (mandatory opener: live version, backup marker, pending logs, audit stamp, config presence)
- Need the whole library → `uv run zel items --json`
- Need specific items → `uv run zel items KEY1 KEY2 --json`
- What changed since the last backup → `uv run zel items --since <backup version> --json`
- How would an entry look in a bibliography → `uv run zel items KEY --bib`
- Survey the tag vocabulary → `uv run zel tags --json`
- See the collection tree → `uv run zel collections`
- Find problems (mess, gaps, duplicates) → `uv run zel audit` then read `<data dir>/audit/*.json` and `audit-report.md`
- Triage only recent arrivals → `uv run zel audit --since <backup version>`
- Check the library against the tag registry → `uv run zel audit registry` (needs `taxonomy.yaml`)
- Check the citekey join with the writing vault → `uv run zel audit citekeys` (needs `citekey_sources` in `config.yaml`)
- Metadata candidates for an item → `uv run zel lookup crossref KEY --json` (or `arxiv`); read its PDF → `uv run zel lookup fulltext KEY` (`--image` for scans)
- First-run onboarding → the `zelador-setup` skill; design or revise `taxonomy.yaml` → the `zelador-taxonomy` skill; run a cleanup/bulk-edit session → the `zelador-review` skill; triage what arrived since the last session → the `zelador-intake` skill
- Bulk analytics or unsynced annotations → `uv run zel local "<SQL>" --json` (read-only snapshot of the desktop's SQLite)
- Before proposing any change → `uv run zel backup` (verified no-op when nothing changed)
- Have an approved changeset → `uv run zel validate <changeset.json>` (writes a version-pinned plan to `<data dir>/plans/`)
- Ready to execute a plan → `uv run zel apply <plan id> --dry-run` first, always; then `uv run zel apply <plan id>`
- Regret a session → `uv run zel undo <plan id> --dry-run`, then without
- Apply crashed mid-run (pending entries in `zel status`) → `uv run zel debug reconcile <session>`
- Library object mangled beyond undo → `uv run zel debug restore <backup id> KEY1 KEY2` (last resort — overwrites current state)
- The library is heavy and the quota is finite → `uv run zel compress scan --limit 20` (measures; installs nothing), then `uv run zel compress swap <run> --dry-run`, then without
- Regret a swap → `uv run zel compress restore <run>` (the originals live in the run directory, not in a backup)
- Check credentials/identity → `uv run zel debug whoami`
- Where do files live → `uv run zel debug paths`
- Inspect a raw API response → `uv run zel debug probe "items?limit=1"`

## Rules

- Never read `.env`; the CLI consumes it itself.
- Reads are cheap but not free: a full dump is ~40 requests. Prefer `--since` scoping and the latest backup/audit files in `<data dir>` (see `zel debug paths`) over re-dumping.
- The change loop runs in listed order, once per approved plan: `backup` → author changeset JSON in `<data dir>/changesets/` → `validate` → user approves per intent group in chat → `apply --dry-run` → `apply`. If any group is rejected, trim the changeset and re-validate — plans are never edited by hand.
- The closed changeset op vocabulary is defined once, in `OPS` in `zelador/write/contracts.py`; no delete op exists — trash only. Command flags and examples live in `--help`, not here.
- `zel compress` is outside the changeset machinery: it writes file bytes, so no plan pins it, `zel undo` cannot reverse it, and `zel compress restore` is its only undo — which means the run directory holds the only copy of the originals until you are satisfied. Zotero must be closed for a swap, and every swapped file re-uploads on the next sync — reclaiming N MB of quota costs N MB of upload first. Compression is lossy: images resample to the preset's DPI, so it is right for reading and wrong for print-scale work or re-OCR.
- Zotero's own annotations live in `zotero.sqlite` and are anchored by page index and rectangle, so a rewrite that preserves page geometry leaves them on the same words; annotations *embedded in the PDF* (`isExternal = 1` in `itemAnnotations`) live in the file and are what the scan's annotation-count check protects. Both were verified against this library before the check existed — see PR #33.

## Development

- Branch → TDD → `/review` → merge; `uv run pytest -q`, `uv run ruff check zelador tests`.
- Tests run against `tests/conftest.py`'s `FakeZotero` over `httpx.MockTransport` — no network.
