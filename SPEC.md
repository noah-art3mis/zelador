# zelador — Specification

Repo: `zotero-driver`. Package and CLI: `zelador` / `zel` (Portuguese for "caretaker").

## What it is

A deterministic Python CLI over the Zotero Web API for reading, auditing, and reorganizing a personal Zotero library. Claude Code is the brain; the CLI is the hands. Classification and proposal-making happen in interactive Claude Code sessions; the CLI provides the safe, deterministic read/validate/apply machinery. The chat is the approval UI.

Two jobs share that machinery: a one-time **cleanup** of the existing library, and a recurring **intake** session that brings each batch of newly added items up to the same standard.

The environment it assumes: the Zotero desktop app runs on Windows, syncing continuously with zotero.org. Its data directory holds `zotero.sqlite` — the app's own local database, and therefore a full replica of the synced library — plus a `storage/` folder containing the attachment PDFs. The CLI runs under WSL on the same machine: canonical reads and all writes go to the Web API at zotero.org; the Windows-side files are reachable read-only through the `/mnt/c` mount.

Explicitly out of scope: building another citation manager, writing to Zotero's SQLite database, a standalone scheduled pipeline (may be revisited later by reusing the same CLI), realtime processing, duplicate detection UI (Zotero's own is adequate for now).

Distribution is a GitHub clone: the repo — CLI, project skills, example configs — is the install unit (`uv sync`, open Claude Code in the repo, run `/zelador-setup`); there is no published package.

## Core principle

The agent proposes; deterministic code validates; the user approves; every applied change is logged and reversible. Writes either succeed verifiably or fail loudly. Reversibility carries one honest bound: undo of trash-based operations expires when Zotero's desktop auto-empties its trash (30 days by default) — past that horizon, the backup is the restore path.

## Prototype posture

This is a prototype, not a product. Optimize for conciseness, understandability, clean architecture, and simplicity — in that spirit, not for bulletproofness. Concretely: no defensive guards against states the design already makes impossible, no retry/fallback layers beyond what the spec names (429 retry, version pins), no configurability beyond what a real session needs, no premature abstraction. Small files, pure functions, obvious names. The safety model is the one place rigor is non-negotiable — everything else should be the simplest code that reads well. When bulletproofing and simplicity conflict outside the safety model, choose simplicity.

## The author's toolchain (what this was built to serve)

The concrete tools and practices this project exists to support, and the exact seams each one drives. A fresh adopter needs none of them beyond Zotero and an agent — every seam is generic and configurable — but this list records the instance they were designed against:

- **Zotero desktop (Windows) + zotero.org sync on tablet** — the library itself; coloured tags are the working UI. Seams: Web API as canonical path; SQLite replica for `zel local`; `storage/` PDFs for fulltext; `tagColors` sync.
- **APA 7** — citation style for all writing. Seams: default `--style` for `--bib`; metadata-completeness rules calibrated per item type to APA requirements.
- **Better BibTeX + Obsidian + LaTeX** — the citekey-joined writing pipeline; the narrative and every seam it drives live in Downstream reference workflow, immediately below.
- **Claude Code** — the driving agent. Seams: `--json` NDJSON contract, project skills, bounded fulltext output, API-key redaction.
- **WSL** — where the CLI runs. Seams: `/mnt/c` read-only access to Zotero's files; the desktop local API discarded as unreachable.

## Downstream reference workflow

The concrete writing pipeline this tool is built to serve: Better BibTeX (BBT — a Zotero plugin that assigns every item a stable citation key, or citekey, and keeps `.bib` bibliography files auto-exported) exports the library into an Obsidian vault (Markdown note-taking app). Writing happens in the vault, with the Obsidian citations plugin inserting citations as `[[@<citekey>]]` wiki-links in the text (it also maintains `@<citekey>.md` literature notes). For LaTeX submission, pandoc converts the Markdown to TeX — mangling the links into `{[}{[}@key{]}{]}` — and a perl one-liner kept in the vault rewrites them to `\citep{key}`; per-paper `.bib` files carry the same citekeys, and the result is finished in Overleaf. Everything joins on the citekey — bib entry ↔ wiki-link ↔ note filename ↔ `\citep`.

BBT derives citekeys from creators+title+year unless a `Citation Key: <key>` line in the item's `extra` field pins them; unpinned keys silently recompute when those fields change, which is exactly what enrichment does. The support is two generic read-only seams, none Obsidian-specific — zelador never writes pins itself; pinning is done in Zotero via Better BibTeX's own "Pin BibTeX key" (the `pin_citekey` op was built and removed at the user's decision — issue #21, branch `pin-citekey`):

- **Citekey sources (config)** — `citekey_sources` is a list of paths: `.bib` files are parsed for their entry keys; anything else is a file glob whose matches are scanned for citekeys — inline `[[@<citekey>]]` wiki-links in Markdown, `\cite`/`\citep`/`\citet{...}` commands in TeX (comma-separated keys included), and `@<citekey>.md` basenames. This is the only place the workflow enters the tool — any consumer that references items by citekey (pandoc, org-roam, a bare LaTeX repo) plugs in the same way. Unset, everything below is inert.
- **`zel audit citekeys`** — cross-references the sources against the library (audit check 5); its `cited_unpinned` findings are the list to pin in Zotero before an enrichment session.

The Web API never sees unpinned keys, so the configured `.bib` export is the authority for an item's current citekey; bib entries match back to items by DOI, falling back to normalized title+year.

## Architecture decisions

- **Read path** — Zotero Web API (v3) is canonical for reads and writes. The local SQLite replica is a secondary, explicitly-named path (`zel local ...`) for bulk analytics and direct PDF file access only; `zel local` snapshots the database file itself and queries the copy, never the live file. The Zotero data directory is auto-discovered per platform (`~/Zotero` on Linux/macOS, `%USERPROFILE%\Zotero` on Windows, mounted Windows profile under WSL — here `/mnt/c/Users/noah_/Zotero`), overridable in config.
- **Write path** — Web API only; never write to SQLite — with one named exception: `zel compress` replaces attachment *files* under the Zotero storage directory (never the database, never through the API). It is the only command whose effect `zel undo` cannot reverse, which is why it carries its own inverse; see Command surface → Storage. Zotero uses optimistic concurrency: every object, and the library as a whole, carries a version counter, and a write conditioned on a stale counter is rejected. Batch item writes carry each item's `data.version` (per-object conflicts arrive in the response's `failed` map) and no library-level `If-Unmodified-Since-Version` — a library-wide header would let any unrelated change 412 the whole batch. The library-level header is used only on endpoints that require it (e.g. the settings endpoint used for `tagColors`), which fail whole-operation.
- **HTTP client** — Hand-rolled thin client on `httpx` (~350 lines), not pyzotero — rationale and revive condition in Discarded ideas. Crib pyzotero's `_client.py` version-header conventions and test suite as reference — pyzotero is Blue Oak 1.0.0 (permissive, MIT-compatible); crib from current releases, not old GPL-era versions.
- **Client musts** — Paginated full-library dump following `Link: next` headers; batch reads of specific items via `itemKey` in 50-key chunks (post-apply verification and undo state-checks fetch exactly the touched items); batch writes — items and collections alike — chunked at 50 parsing the per-object `success`/`unchanged`/`failed` response maps; honor `Backoff`/`Retry-After` including on 200s, with real retry of 429'd requests; an explicit timeout on every request, so a dead socket fails loudly instead of hanging; surface version conflicts per item key; endpoint builder constructs `/users/<ZOTERO_USER_ID>/` paths only — group libraries are unreachable by construction, and setup verifies the key's identity and write access via `/keys/current`; the `Zotero-API-Key` header is redacted from exceptions, traces, and logs — CLI output is agent-read by design, so an unredacted error would leak the key into the agent's context.
- **Auth** — `.env` at repo root: `ZOTERO_API_KEY`, `ZOTERO_USER_ID=11868292`. Never read back into agent context; consumed by the CLI process only. Default setup has the user write `.env` themselves so the key never enters the chat; a user who prefers convenience may paste the key to the agent to write, accepting that it enters the transcript.
- **Stack** — Python via `uv`; TDD against a mocked httpx transport.

## Safety model (three layers)

1. **Pre-session snapshot** — `zel backup` dumps every item's full JSON, including trashed items (`includeTrashed=1`), plus every collection object and the `tagColors` setting — items alone would lose the tree: item JSON carries collection membership keys, but names and parent links live only on collection objects — to `<data dir>/backups/<timestamp>.jsonl`, one object per line. A conditional GET (`If-Modified-Since-Version` with the last backup's library version) makes it a verified no-op when nothing changed. Each expanded plan records the backup it was validated against plus the library version at validation; `zel apply` requires that exact backup, not merely a recent one. This is the last-resort restore, and restore is a named path, not an idea: `zel debug restore <backup> <key...>` pushes the backed-up JSON of exactly the named objects back through the standard write machinery — deliberately version-unpinned, because last-resort recovery overwrites whatever state the accident left — logged like any session and drilled once in M3b. Routine undo replays the change log (layer 2) instead: Zotero's server keeps no history — item deletions go to a restorable trash, but tag removals and field overwrites are unrecoverable server-side.
2. **Write-ahead change log** — a session is one `zel apply` run of one plan; its log file is named by the plan id. Before each write request, the session log gets a `pending` entry per operation carrying the old state; after the response, each entry is marked `applied`, `unchanged`, or `failed` from the per-object result maps, with the new version. A crash mid-apply therefore never loses the undo record. `zel apply` refuses to start while any session log has unresolved `pending` entries; `zel debug reconcile <session>` clears them deterministically — fetch the touched items, compare against each entry's logged old and new state, and mark it `applied` or `failed` accordingly. `failed` entries end that operation's story — recovery is never an in-place retry but a fresh cycle: carry the failed operations' intents into a new changeset, `zel validate` against the moved library, approve the new plan; plans are never edited by hand. `zel undo <session>` refuses a session whose log still holds `pending` entries (reconcile first), replays only `applied` entries backwards, and before reversing each one verifies the item's current state still equals the logged new state — anything else is reported as an undo conflict and left untouched; reversed entries are marked `undone`, so the log records that outcome too.
3. **Validator hard rules** — no deletions ever, items and collections alike (the agent may only propose trashing — Zotero 7 gave collections a trash of their own — never purge); changesets touching >200 objects refuse to apply without an explicit `--big` flag; writes only to fields the changeset schema allows — `extra` is never a `fill_field` target — it is plugin territory, Better BibTeX pins citekeys there — and `fill_field` targets are checked against the item type's actual fields via the API schema endpoints (`/itemTypeFields`, cached per session) rather than hardcoded lists.

Free fourth layer: the local Windows SQLite replica is itself a full copy sync could restore from.

**What the three layers do not cover: file bytes.** Every layer above records item JSON. `zel backup` never holds an attachment's content, so a replaced file cannot be recovered from a backup and `zel undo` has nothing to replay. `zel compress` is therefore the one command that ships its own reversal rather than relying on the shared machinery: it never overwrites an original in place — it moves it into the run directory and installs the replacement — and `zel compress restore` moves it back.

What that buys, precisely: the original is never *destroyed* by a swap, and neither direction will move a file whose md5 differs from what the scan recorded, so a half-copied file, a file another run has since replaced, and a file edited outside zelador are all refused rather than installed. What it does not buy: the run directory is the only copy of those originals, so deleting one after a swap is unrecoverable; and a swap is all-or-nothing at its *gate*, not in its execution — nothing rolls back an earlier entry if the sixth of ten fails to move, though the entries that did move remain individually restorable. Compression is also lossy by construction (images resample to the preset's DPI), and an image-only scan with no text layer is verified on page count, annotation count and size alone — there is no check that its pixels still carry the same information.

## Changesets

Changesets are **symbolic intents**, not expanded edits: a closed operation vocabulary — `merge_tag`, `add_tag`, `remove_tag`, `fill_field`, `clear_field`, `set_creators`, `set_item_type`, `create_item`, `add_to_collection`, `remove_from_collection`, `create_collection`, `rename_collection`, `move_collection`, `trash_collection` (propose-only), `trash_item` (propose-only) — defined by a schema in the repo and grown only by editing that schema. `zel validate` expands intents against the live library into an exact per-item plan pinned to item versions; the expanded plan is what the user approves and what `zel apply` executes. Version pins make stale plans fail loudly per item instead of drifting silently.

**Expansion semantics.** Zotero treats `tags` and `collections` as complete arrays — a partial write silently removes whatever it omits. So every tag/collection operation expands by read-modify-write of the full array, and the plan records both old and new arrays per item. Operations compose: when one plan touches the same item more than once (a pin plus field fills, a merge plus an add), expansion coalesces them into a single write object carrying the composed new state — sequential writes to one item would invalidate each other's version pins. Log entries stay per-operation and share their write's resulting version. `merge_tag` rewrites each carrying item's tag array (add canonical, drop aliases, preserve everything else); no global tag delete is needed — a Zotero tag ceases to exist once no item carries it. `create_collection` operations carry a precomputed client-generated object key, so a retried request cannot create a duplicate and undo always knows the key; undoing a `create_collection` trashes the collection by that key, never a hard delete. `rename_collection` and `move_collection` are read-modify-write of the collection object's `name` and `parentCollection`, version-pinned like any write, old and new values in the plan. `trash_item` and `trash_collection` are a PATCH of `deleted: true` — the undo is exactly `deleted: false`; trashed items remain listed under `/items/trash`, and trashing a collection leaves its items and their memberships intact. The correction ops (issue #24) assume nothing about how an item arrived: `clear_field` writes the empty string (which the server stores as field removal — the missing half of `fill_field`, since a wrong value must be removable, not just overwritable); `set_creators` replaces the whole creators array, validated against the item type's creator types; `set_item_type` changes the type and, because the server validates the *merged* object, clears every stored field the new type does not allow in the same write — and refuses when an existing creator type does not fit the new type (`set_creators` first). `create_item` carries a precomputed client-generated key exactly like `create_collection` — idempotent create, undo trashes by that key — validating fields against the item type, tags against the registry (exclusive families included), and collections against the tree; its optional `attachment` selector adopts an existing standalone attachment by writing its `parentItem` in the same plan, which is how a PDF that imported without metadata becomes a real item. Whether a citekey-affecting edit *should* wait for a pin (done in Zotero via BBT) is the session's call, informed by the citekey audit (issues #19, #21).

**Contracts.** Three versioned JSON shapes, defined once as dataclasses + schema in the repo: `changeset.v1` (the intents), `plan.v1` (a header binding the plan to its backup id and validation-time library version; per-item operations each with an operation id, item key, pinned item version, old state, new state, and risk tier; plus the optional `tagColors` settings entry), and `log.v1` (operation id → pending/applied/unchanged/failed/undone + resulting version). Approval in chat is per intent group — a group is one intent together with everything it expanded into — but apply is all-or-nothing per plan: if any group is rejected, the agent trims the changeset and re-validates into a fresh plan — what `zel apply` executes is always the fully-approved plan file, byte-for-byte. Plan ids — and therefore session log names — are `<UTC timestamp>-<slug>`.

Enrichment logic is CLI-side (deterministic, cached, fixture-tested) — the agent's role is judgment: deciding whether a lookup candidate truly matches an item and emitting `fill_field` intents.

## Approval flow

```
agent reads library → proposes changeset (JSON in <data dir>/changesets/)
→ zel validate (against taxonomy.yaml + hard rules)
→ agent renders compact summary in chat, user approves per group
→ zel apply --dry-run (always) → zel apply
→ change log written
```

Risk tiers: filling an empty field is low-risk; overwriting a non-empty field, removing tags, and major moves are high-risk. Every change is approved — tiers set how much scrutiny a group deserves in review, not whether it is reviewed.

## CLI design

- **Framework**: Typer, single console script `zel = "zelador.cli:app"`. Everyday verbs top-level, grouped with `rich_help_panel`; utilities under one nested `zel debug` sub-app. Modern `Annotated[T, typer.Option(...)]` signatures. English commands, kebab-case options, `--flag/--no-flag` booleans, positional argument for the primary subject.
- **Output**: human output on stdout; diagnostics and defaulting notices on stderr (`err=True`). Every read command takes `--json` emitting newline-delimited JSON, one object per line (jq-friendly; the form agents consume); mutating commands take it too, emitting a final outcome object (counts plus per-key failures with the API's code and message) so the agent never parses prose to decide what to do next. A `--verbose` flag traces each request (method, URL, status, version headers — auth redacted), and backoff waits are announced on stderr so honouring `Backoff`/`Retry-After` never looks like a hang. Color respects `NO_COLOR` and disables when piped. Exit codes: 0 success, 1 operational failure, 2 bad input / user abort.
- **Safety idioms**: `--dry-run` on every mutating command printing exactly what would happen; `typer.confirm` gate unless `--yes`; forecast banner (item/request counts) before anything expensive or rate-limited.
- **Docs**: README with aligned command table and copy-pasteable `uv run zel ...` blocks; every command docstring has an Examples block; `CLAUDE.md` carries a "situation X → run command Y" routing table for agent use.
- **Layout**: flat package (`zelador/` at repo root), hatchling, `.env` + committed `.env.example` (and `taxonomy.yaml` + committed `taxonomy.example.yaml`, same pattern), thin Typer bodies over pure functions, `CliRunner` tests asserting exit codes and output. Deliberate deviation from house style: data lives in `platformdirs` user data dir, not in-repo, because this repo is public.

### Command surface

Kept deliberately small: variants of the same operation are flags or subcommand arguments, not new verbs (`zel items <keys>` is the batch read, `--bib` is the bibliography rendering, `fulltext` is a lookup source). Panels are the `rich_help_panel` groups in `--help`.

- **Library**
  - `zel status` — one-screen session orientation, the skills' mandatory opener: live library version (the command's single API request), last backup's timestamp and version (the intake `--since` marker), item/tag counts from the latest backup, unresolved `pending` session logs, any files in `log/` that are not session logs, latest audit report and its version, and whether `config.yaml`, `taxonomy.yaml`, and `citekey_sources` are configured; `--json`. If the API request fails it prints the local half plus the error — the one named exception to failing loudly, because "API unreachable" is exactly what a session opener must be able to say.
  - `zel items [keys...]` — full paginated dump, or batch read of the given keys; `--since <version>` limits the dump to items added/modified after that library version (Zotero's native `?since=`); `--json` NDJSON; `--bib` (`--style`, APA default) renders bibliography entries server-side (`include=bib`).
  - `zel tags` / `zel collections` — read tag list (with counts and type) / collection tree, `--json`.
  - `zel local <sql>` — read-only analytics: raw agent-authored SQL against a fresh snapshot of Zotero's SQLite. Every run recopies the database with its `-wal`/`-journal` siblings (the desktop writes the live file continuously) and gates on `PRAGMA integrity_check` — a torn copy fails loudly, never answers.
- **Findings**
  - `zel audit [check]` — run all audit checks or one; `--since <version>` scopes findings to items changed after that library version (see Audit); writes JSON per check + `audit-report.md` to the data dir.
  - `zel lookup crossref|arxiv|fulltext` — deterministic enrichment lookups from a named source, cached, candidates with scores; `fulltext <key>` tries server fulltext first, local `pypdf` extraction as fallback; output bounded by default, `--full` returns everything, truncation flagged in `--json`; `--image` renders page one to PNG for visual inspection of scanned PDFs.
- **Change loop**
  - `zel backup` — full-library JSONL snapshot (items, collections, `tagColors`) to the data dir.
  - `zel validate <changeset>` — check symbolic intents against `taxonomy.yaml` + hard rules; expand into a version-pinned per-item plan written to `plans/`.
  - `zel apply <plan>` — execute an expanded plan; `--dry-run` first-class; refuses unless the plan's pinned backup exists; `--big` for >200 items.
  - `zel undo <session>` — replay a session's change log backwards.
- **Storage**
  - `zel compress scan [keys...]` — compress candidate PDFs into a run directory and report what each would cost; reads the library, writes only into the run. `--preset` (Ghostscript quality), `--min-bytes`, `--min-saving`, `--text-tolerance`, `--limit`, `--json`. A candidate is accepted only if the output keeps the page count, loses no annotation objects (embedded highlights live in the file, and a compressor that flattened them would otherwise look like a clean win), keeps a text layer within tolerance, and clears the saving floor — rejected output is deleted rather than left staged. A candidate that cannot be read or compressed is recorded as a rejection and the scan continues.
  - `zel compress swap <run>` — install a run's accepted files, moving each original into the run's `originals/` first; `--dry-run` first-class, `--yes` gate. The gate is all-or-nothing: one blocked entry refuses the run. It blocks when a storage file's md5 no longer matches the scan (the staged copy was built from bytes that no longer exist) or when a staged file's md5 does not match what the scan produced (a move that died halfway leaves a file that exists and is wrong). Already-swapped entries are skipped, so re-running is safe.
  - `zel compress restore <run>` — the inverse: originals return to storage, compressed files return to staging, so a restored run can be swapped again without re-scanning. Blocks on a quarantined original whose md5 does not match the scan, and on a storage file that is not the one this run installed — restore the later run first. A *missing* storage file is not a blocker: that is what a swap interrupted between its two moves leaves, and restoring is the repair.
- **Utilities**
  - `zel debug ...` — inspection and recovery utilities: config paths, cache state, raw API probe, `whoami` (`/keys/current` identity), `reconcile <session>` (resolve a crashed apply's pending entries), `restore <backup> <key...>` (layer-1 last resort — see Safety model).

Expected use: the agent opens with `zel status`; in a `zelador-review` or `zelador-intake` session it leans on **Library** reads throughout for context (`--json` mode, piped); **Findings** runs early — audit at session start, lookups while drafting enrichment intents; the **Change loop** runs late and in listed order, once per approved plan (`backup` → `validate` → `apply`, with `undo` reserved for regret); **Utilities** only when something misbehaves. Humans run the same commands ad hoc — most often `zel audit` and `zel items --bib` — which is why everything stays top-level rather than nested under sub-apps.

## Data layout

The repo is public. Personal library data never lives inside the working tree — it lives in the platform-native user data directory, resolved via `platformdirs` (`~/.local/share/zelador/` on Linux, `~/Library/Application Support/zelador/` on macOS, `%LOCALAPPDATA%\zelador\` on Windows), overridable with `ZELADOR_DATA_DIR`:

```
<data dir>/
  backups/     pre-session full-library JSONL snapshots
  audit/       audit output JSON + generated report
  changesets/  proposed changeset JSON files
  plans/       expanded version-pinned plans emitted by `zel validate`
  cache/       lookup response cache (Crossref/arXiv)
  log/         append-only per-session change logs (log.v1)

Unlike the other subdirectories, `log/` is shared: any one-off script writing an audit trail
lands its file beside the session logs. So a file there is a session log only if its first line
is a header declaring `schema: log.v1` — every reader checks that before folding an entry, and
`zel status` names the files that fail the check rather than skipping them silently.
  compress/    one directory per `zel compress` run: report.json, staged/, originals/
```

A compress run directory is the only place zelador holds *file bytes* rather than JSON: `staged/<key>/<filename>` is a compressed candidate awaiting install, `originals/<key>/<filename>` is the untouched original a swap moved out of the way. That original's presence is what marks a key as swapped — no separate state file can disagree with the disk — and it is exactly what `zel compress restore` moves back. Deleting a run directory after a swap discards the only copy of those originals.

Committed: code, `SPEC.md`, `LICENSE` (MIT — all deps and the pyzotero reference are permissive), `taxonomy.example.yaml` (the starter registry), `config.example.yaml`. Never committed: `.env`, `taxonomy.yaml` (the user's registry — a personal vocabulary), `config.yaml`, any library dumps. A defensive `.gitignore` for `.env`, `taxonomy.yaml`, `config.yaml`, and `data/` exists as a second net regardless.

Non-auth config lives in `config.yaml` at repo root (committed `config.example.yaml`, same pattern as `.env` and `taxonomy.yaml`), capped at three keys until a real session needs a fourth: `zotero_data_dir` (overrides the per-platform auto-discovery), `citekey_sources` (see Downstream reference workflow), and `style` (CSL style, `apa` default). `style` governs rendering defaults — `--bib` and the audit's rendered-entry check — never the metadata-completeness field table, which stays APA-calibrated in code. The zelador data dir itself remains env-only (`ZELADOR_DATA_DIR`).

## Tag taxonomy

Namespaced tags of the form `family:value` (lowercase, hyphens for compound values), controlled by a registry file (`taxonomy.yaml`). The registry declares the allowed families, each family's canonical tags, their meaning, known aliases, and optional colours; the validator only permits tags listed there. The repo commits `taxonomy.example.yaml` — a starter registry carrying the default families and colours — which setup copies to `taxonomy.yaml` for the user to opt into and edit; the live registry is the user's file, never the repo's.

- Initial families: `status:` (workflow state), `topic:` (subject vocabulary). Further families (`rating:` (subjective valuation — fav/core/skip), `method:`, `use:`, ...) are added only if the audit shows a real need – the family list lives in the registry, not in code, and the registry is designed from real audit data (M2), not invented up front.
- Colours: Zotero pins up to 9 coloured tags atop its tag-selector pane and gives them number-key shortcuts — prime UI real estate for workflow tags. A registry entry may declare a colour (`#RRGGBB`) and position. Colours are chosen in Zotero and *recorded* here, so the validator checks the format rather than dictating a palette — it originally accepted only the Okabe-Ito colourblind-safe palette (ref: jfly.uni-koeln.de/color), which meant a user who had already set colours by hand could not write them down, and every plan then carried a repaint of their library nobody asked for. Okabe-Ito remains the recommendation and is what `taxonomy.example.yaml` ships. The one hard refusal is `#F0E442`: coloured tags render as coloured text in Zotero's selector, where that yellow is illegible. The registry is authoritative for the entire `tagColors` array — the setting is one value, so writing it replaces every assignment, including colours hand-set outside the registry (the plan's old value shows exactly what goes). When registry colours differ from the library setting, `zel validate` adds a library-settings entry (old and new `tagColors` value) to the plan alongside the per-item operations; `zel apply` writes it via the Web API settings endpoint (version-pinned like any write), the change log tracks it, and undo restores the previous value. The validator enforces the 9-tag cap; colours are reserved for workflow families (`status:` and kin), never `topic:`.
- User should disable Zotero's "Automatically tag items with keywords and subject headings" setting so the mess stops growing.

Default colour assignments, shipped in `taxonomy.example.yaml` (Okabe-Ito; the copied registry is the user's file, so changing them is editing one line — and if colours were already set in Zotero, the registry should be edited to match them rather than the reverse). Positions double as Zotero's number-key shortcuts, ordered by expected frequency of use. Yellow (`#F0E442`) is deliberately unused — coloured tags render as coloured text in the selector, where yellow is illegible:

- Position 1 — `status:to-read` — `#E69F00` (Okabe-Ito orange)
- Position 2 — `status:read` — `#009E73` (Okabe-Ito bluish green)
- Position 3 — `rating:fav` — `#CC79A7` (Okabe-Ito reddish purple)

Registry shape (`taxonomy.yaml`), kept minimal:

```yaml
families:
  status: {description: workflow state, coloured: true, exclusive: true}
  topic:  {description: subject vocabulary}
tags:
  - tag: status:read
    description: finished reading
    aliases: [read, finished]
    colour: "#009E73"    # optional; Okabe-Ito bluish green; position = order within coloured tags
```

An `exclusive` family allows at most one of its tags per item — `status:to-read` and `status:read` cannot coexist; the validator refuses writes that would violate it, and the conformance audit (check 6) reports the stock's existing violations.

Aliases are the adoption mechanism: a registry tag lists the names a library's pre-existing tags used for the same concept, and approved `merge_tag` changesets fold them into the canonical vocabulary. Aliases are valid as selectors in changeset intents (so `merge_tag` can name what it replaces) but never as proposed output — the validator only ever writes canonical tags. The registry is linted at load, before anything expands against it: a duplicated canonical tag, an alias listed under two tags (it would make `merge_tag` ambiguous), or an alias shadowing a canonical name fails loudly.

## Legacy migration (this library)

One-time mappings for the author's existing tags — a fresh adopter skips this section; the mechanism is just the alias machinery above. The current tag system is otherwise dropped wholesale, not transitioned.

- Old status vocabulary, translated to English: `ler` → `status:to-read`, `lido` → `status:read`, `fav` → `rating:fav` (introducing the `rating:` family — the audit confirms what `fav` actually marked by sampling its carriers). `_tablet` is dropped with the rest, not migrated.
- The ~1,200 auto-imported subject tags (arXiv categories, newspaper sections, publisher keywords) are rewritten against the registry as `topic:` tags, in approved batches (M4).

## Collections philosophy

The starting hypothesis for M5, to be confirmed against M1 audit data — rules normative now, shelf names decided then. Division of labour: **collections are entry points** (projects and browsing shelves), **tags carry the cross-cutting truth** (subject, status). Zotero copies tags with items across libraries but not collection placements — subject knowledge lives portably in `topic:` tags; shelves are disposable views over it.

```
projects/<name>/...        one per active output (papers, the capstone, courses);
                           subcollections mirror the output's own structure (chapters, screening stages)
topics/<area>/<subtopic>   coarse browsing shelves — few areas, at most one subtopic level,
                           matching the shape of today's tree, which M5 maps rather than tears down
archive/<name>/...         finished projects moved here wholesale, reading lists preserved
```

- Depth: `topics/` stops at one subtopic level; only `projects/` earns deeper nesting, and only mirroring its output. Distinctions finer than a subtopic are `topic:` tags, not collections.
- A shelf earns existence when browsing it is useful; a tag earns existence when filtering by it is useful.
- `archive/` takes projects, never items: a finished project's collection moves under `archive/` intact; its items stay live on their shelves and tags.
- Unfiled = untriaged. Zotero's built-in Unfiled Items view is the inbox; intake files each new item onto at least one shelf. Project membership happens when a project actually uses the item, not at intake.
- Dynamic views (`status:to-read` × one `topic:`, say) are desktop saved searches, not more collections — a user habit the spec names, not tool surface (the saved-searches API stays discarded).

The redundancy between shelves and `topic:` tags is accepted, not fought: shelves serve browsing (the tablet especially, where tag filtering is clumsy), tags serve filtering and validation. Intake guards the join for new items; the collection-hygiene audit catches drift in the stock.

## Audit (`zel audit`)

Emits one JSON file per check under `<data dir>/audit/` (machine-readable, diffable between runs) plus a generated `audit-report.md` summary. Each check's JSON is stamped with the library version and timestamp it was computed at — the anchor that makes run-to-run diffs meaningful.

`--since <version>` scopes the findings to items added or modified after that library version — the intake session's default view. Zotero's native `?since=` parameter does the item filtering server-side; checks that need whole-library context to judge an item (duplicates compare against everything, tag mess clusters across the full tag list) still read the full library but report only findings involving in-scope items. The marker is not new state: it is the previous backup's recorded library version, read from `backups/` and surfaced by `zel status`.

1. **Metadata completeness** — per item, missing DOI / date / creators / publication, scored by item type (a webpage legitimately lacks a DOI; a journal article doesn't). Judged against citation needs, not completionism — literally: the audit renders each item's actual bibliography entry server-side (`include=bib` with a CSL `style=`, APA by default — the author's general style) and a visibly broken entry is the finding, alongside the field-level rules. Those live as one data table in code, keyed by Zotero `itemType`: which fields are required, and which field counts as the "publication" (`publicationTitle`, `bookTitle`, `conferenceName`, `publisher`, ...). Requirements are calibrated to what APA 7 asks per type, covering the types the library actually holds — all on top of creators, date, and title: journal articles (`publicationTitle`, volume, issue, pages, DOI); preprints (repository, arXiv id or DOI); books (publisher — APA 7 dropped place); book chapters (`bookTitle`, editors, pages, publisher); conference papers (proceedings name, pages, DOI); newspaper and magazine pieces (publication, exact date); blog posts and webpages (site name, URL, exact date); videos and podcasts (channel or host, platform, URL, date); reports and theses (institution, report number or thesis type). An item whose type has no rule is itself a finding — never silently skipped. The capstone project's BBT-exported `.bib` files are the concrete reference for which fields real citations exercise. Includes standalone attachments: PDFs with no parent item, invisible to bibliographies.
2. **Tag mess** — cluster near-duplicates and case-duplicates (`Artificial Intelligence` / `artificial intelligence` / `AI`, four casings of `machine learning`), separate automatic from manual tags (the API marks tag type).
3. **Collection hygiene** — items in no collection, empty collections, duplicate sibling names (two `Clickbait` under Detection), orphaned subtrees.
4. **Duplicate items** — same DOI or near-identical title+year.
5. **Citekey integrity** (only with `citekey_sources` configured — see Downstream reference workflow) — orphaned citations — citekeys found in a source (wiki-link, `\cite` command, or note filename) that match no bib entry (evidence of past drift) — and cited-but-unpinned items: citekeys present in a source whose item lacks the `Citation Key:` pin, i.e. the set a metadata edit could break.
6. **Registry conformance** (only with `taxonomy.yaml` present) — the steady-state check backing intake's promise: library tags the registry knows neither as canonical nor alias, top-level items carrying no `status:` tag (the tag-side untriaged detector, mirroring Unfiled on the collection side), exclusive-family violations, and drift between registry colours and the library's live `tagColors` (Zotero-side edits would otherwise go unnoticed until the next colour-touching validate).

## Enrichment (missing metadata)

Sources, composable, all proposal-only through the standard changeset flow:

- **Crossref API** (the DOI registry's metadata service; free, keyless) — fill journal/date/pages by DOI; fuzzy title+author match to find missing DOIs.
- **arXiv API** — abstracts and canonical versions for the arXiv-heavy portion.
- **Server fulltext first** — Zotero has already indexed synced PDFs; `GET /items/<key>/fulltext` returns the extracted text in one request. Try this before any local parsing.
- **PDF extraction** (`pypdf`) — text from local PDFs (the ~2 GB Zotero `storage/` folder, reachable from WSL), fallback for orphan attachments and unsynced files only.

Fulltext output is bounded for agent consumption regardless of source: the default is a metadata-sized head (~first page — title, authors, venue, DOI live there); `--full` returns everything. Locally the head is a real `pypdf` page; for server text (one unpaginated string) it is a character-budget equivalent. The `--json` object always states the source (server or local), whether it was truncated, and the page totals, and the local path includes the PDF's on-disk location — so when the head isn't enough, the agent escalates to `--full` or reads the file itself. The cache keeps the full server response; truncation is display-time, so escalating costs no extra request. The last resort is visual: for scanned or garbled PDFs where text extraction yields nothing useful, `--image` renders the first page to a PNG in `cache/` and prints its path for the agent to view directly (via `pypdfium2` — permissively licensed, unlike PyMuPDF's AGPL).

Crossref disagreements with existing metadata (wrong year, mangled authors) are flagged in the audit report only — never auto-fixed.

Lookup caches never expire; `cache/` is pruned by hand (prototype posture).

Enrichment edits exactly the fields Better BibTeX derives citekeys from (creators, title, date) — before an enrichment session touches them, `zel audit citekeys` names the cited-but-unpinned items so the user can pin them in Zotero (BBT's "Pin BibTeX key") first. Advisory by design — issues #19, #21.

## Skills (shipped with the repo)

The repo is public and Claude Code is the intended driver, so the workflows ship as project skills in `.claude/skills/`, written for any user, not just the author.

Every skill opens with a deterministic orientation preamble before proposing anything: `zel status` first — mandatory, common to all — then per-skill context: `zelador-taxonomy` reads the tag-mess audit JSON and the current registry; `zelador-review` and `zelador-intake` read the latest audit report and `taxonomy.yaml`; `zelador-setup` skips what cannot exist yet on first run.

- **`zelador-setup`** — first-run onboarding: create a Zotero API key (zotero.org → Settings → Security → Applications); write `.env` — default flow has the user edit it themselves so the key never enters the chat, pasting it to the agent is an accepted shortcut if the user prefers; discover the user ID — `zel debug whoami`; verify the connection; copy `taxonomy.example.yaml` → `taxonomy.yaml` and walk the user through opting into families, tags, and colours; recommend disabling auto-tagging.
- **`zelador-taxonomy`** — design or revise `taxonomy.yaml` interactively from real audit data — cluster existing tags, propose canonical vocabulary + aliases, record decisions.
- **`zelador-review`** — the full-library session: backup → audit → interpret findings → propose changesets → validate → in-chat approval → dry-run → apply → log. The cleanup campaign's workhorse; after M5, run occasionally as a health check.
- **`zelador-intake`** — the steady-state session: same loop as `zelador-review`, scoped to the delta. `zel status` supplies the marker (the previous backup's library version); `zel backup`, then `zel audit --since <marker>` and `zel items --since <marker>` to triage each new arrival — `status:to-read` plus `topic:` tags from the registry, metadata gaps filled via `zel lookup` — through the standard changeset flow. No new machinery: the skill is a scoping discipline over the same commands.

Skills encode the safety flow so it is followed by construction, not memory.

## Milestones

- **M1** — read client + `zel backup` (incl. trash, collections, `tagColors`) + `zel audit` — tags, metadata, collections, duplicates → JSON + report — plus the `zel local` snapshot machinery (the unsynced annotations live only there).
- **M2** — taxonomy registry (`taxonomy.yaml`) designed together from M1 audit data.
- **M3a** — write machinery — contracts, `zel validate` expansion, `zel apply`, `zel undo` — TDD against mocked transport and golden plan fixtures; no live writes.
- **M3b** — live shakedown: merge obvious case-duplicate tags on a handful of items, then an undo drill and a `zel debug restore` drill on a sacrificial item — both recovery paths proven before anything bigger.
- **M3c** — metadata enrichment (the stated pain: bibliographies): `zel lookup` and the citekey audit (check 5) land here — the audit names cited items to pin (in Zotero) before enrichment touches derived fields.
- **M4** — full tag rewrite against the registry (~1,500 tags → curated vocabulary).
- **M5** — collections restructure, through the same changeset machinery (the collection operations exist for exactly this) — last, because the collection tree is load-bearing for the capstone's LaTeX submissions and restructuring it is a taxonomy conversation.
- **Steady state (after M5)** — cleanup gives way to intake: recurring `zelador-intake` sessions triage what arrived since the last one, keeping new items conformant so the library never needs a second campaign; full `zelador-review` drops to an occasional health check.

Skills land with their first consumer: `zelador-setup` and `zelador-taxonomy` with M2, `zelador-review` with M3b, `zelador-intake` at steady state.

## Discarded ideas

Considered and rejected, with the condition that would revive each:

- **pyzotero adoption** — its batch updates swallow per-object failures and 429s return success without retrying — both on our critical path; 3 of 4 deps dead weight for our subset. *Revive if:* scope grows to attachments/file uploads.
- **Zotero 7 local API (`localhost:23119`)** — read-only mirror of the Web API served by the desktop app — attractive, but unreachable from this WSL setup (binds Windows localhost; NAT mode can't see it), and it requires Zotero running. The SQLite snapshot covers the same ground, including the unsynced annotations (see Library facts). *Revive if:* WSL mirrored networking is enabled and a spike shows it reachable; then `zel local` can share the web client's parsing.
- **Group library support** — personal-library tool; the endpoint builder makes groups unreachable by construction (see Client musts). *Revive if:* a deliberate future group mode, never by accident.
- **`since`-based cached dump refresh** — YAGNI at the current library size (see Library facts) — a full dump is ~20 requests run occasionally; a persistent cache adds invalidation state. Distinct from `--since` scoping on `zel items`/`zel audit` (accepted — see Audit), which passes Zotero's `?since=` straight through per invocation and keeps no state. *Revive if:* dumps become an actual bottleneck.
- **`Zotero-Write-Token` idempotency** — redundant: precomputed client-generated keys already make creates idempotent and give undo the key.
- **`create_note` operation** — was in the vocabulary, but no audit check, skill, or milestone ever proposes a note — surface without a consumer; its idempotent client-key semantics moved to `create_collection` (and later `create_item`, issue #24). *Revive if:* a workflow actually wants to write notes (an annotation-rescue summary, say).
- **Paul Tol palette variants** — three alternate palettes beside Okabe-Ito were configurability no session needs. *Revive if:* Okabe-Ito's hues genuinely fail someone.
- **`--more` fulltext rung** — a middle step (first two pages + last) between the bounded head and `--full`; escalation already costs no extra request, so two rungs plus `--image` cover it. *Revive if:* real sessions show `--full` regularly blowing the context budget.
- **Plan checksums / approval metadata** — prototype posture: the plan file is the approved artifact and is executed byte-for-byte. *Revive if:* multiple approvers or out-of-chat approval flows.
- **Auto-applying low-risk fills** — contradicts the core principle — the user approves every change; no trust mechanism was ever specified. *Revive if:* enrichment runs clean for several sessions and approval fatigue is real.
- **Logging framework, metrics, log rotation** — stderr prose + the change log cover a prototype; the data dir is user-visible and pruned by hand. *Revive if:* the prototype becomes a product.
- **Flat registered tags** — original taxonomy design; superseded by namespaced `family:value` tags with registry-declared colours.
- **Obsidian-side tooling (bib sync, note generation, LaTeX builds)** — Better BibTeX auto-export and the obsidian-citation-plugin already do this; zelador only protects the citekey join they depend on.
- **Configurable citekey scan patterns** — the Markdown wiki-link, TeX `\cite*`, and `.bib` scanners are hardcoded; pattern config is configurability no real session needs. *Revive if:* an adopter's citekey consumer doesn't match the built-in scanners.
- **`pin_citekey` and the cited-but-unpinned validation veto** — both shipped in M3c and were removed at the user's decision: the veto surprised by refusing unrelated title/date fixes (issue #19), and the user doesn't want zelador writing pins at all — BBT's own client UI does that (issue #21; implementation preserved on branch `pin-citekey`). The audit still reports the cited-but-unpinned set. *Revive if:* hand-pinning in Zotero proves too tedious before an enrichment campaign, or an enrichment session actually orphans vault citations.
- **PyPI packaging** — the skills and example-file onboarding are repo-shaped; a bare installed CLI without them defeats the safety design. *Revive if:* the prototype becomes a product with a non-Claude-Code audience.
- **File upload/download, saved searches, `/publications`, OAuth, Atom/export formats, server-side search** — no consumer in any milestone; full-dump-then-filter makes query plumbing pointless surface area. Item creation (issue #24) deliberately stayed metadata-only: the file itself enters through the Zotero client, and `create_item`'s attachment adoption parents what is already there. *Revive if:* a milestone that actually needs one.

## Process

Branch → TDD → `/review` → merge. `uv` for everything. Semantic commits.

## Library facts (as of 2026-07-19 audit snapshot)

2,006 real items; 1,510 tags; sync live to zotero.org (user `noah-art3mis`; user ID in Auth); 1,142 PDF annotations (Dec 2024 – May 2025) exist only locally and never synced — why sync missed them is still to investigate; the rescue design (bake them into derived PDF copies, `zel bake`) is tracked in issue #4; Better BibTeX installed; two group libraries present but out of scope. BBT auto-export is live (`zotero_library.bib`, 1,843 entries, inside the Obsidian vault) and feeds 178 `@citekey.md` literature notes plus inline `[[@citekey]]` citations across the writing notes; zero pinned citekeys library-wide, so every citekey is derived and enrichment-fragile — one orphaned note pair in the vault already evidences drift.
