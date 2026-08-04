---
name: zelador-review
description: Full-library cleanup session – backup, audit, propose changesets, validate, per-group approval, dry-run, apply. Use when the user invokes /zelador-review, wants to clean up, triage, or bulk-edit their Zotero library through the change loop, or asks for a library health check.
---

# zelador review

The full-library session: read the mess, propose fixes, and push approved changes through the change loop. You propose, deterministic code validates, the user approves, every applied change is logged and reversible. The same session run without appetite for changes is the health check – orientation and findings only, stop before authoring.

Orientation (mandatory, in order):

1. `uv run zel status --json` – refuse to continue past unresolved pending sessions (`zel debug reconcile` first) and note the last backup's library version.
2. `uv run zel backup` – a verified no-op when nothing changed; everything downstream pins to this snapshot.
3. `uv run zel audit` (add `--since <backup version>` to triage only arrivals), then read `audit-report.md` and the per-check JSON in `<data dir>/audit/` (paths from `zel debug paths`).
4. Read `taxonomy.yaml` and run `uv run zel audit registry` – canonical tags and aliases constrain every tag proposal. No registry yet? Recommend the `zelador-taxonomy` skill first; without it, tag merges have no canonical targets.

Then work the findings with the user, in passes: interpret the audit into a short list of themes (case-duplicate tags, alias fold-ins, untriaged items, stray collections) and agree which to tackle this session before authoring anything.

The tagging conventions in the `zelador-intake` skill – no default `status:` tag, and the short list of collections still open to new items – bind here too whenever a pass proposes tags or filing.

For each agreed theme:

- **Author** a changeset in `<data dir>/changesets/<slug>.json` – `schema: changeset.v1`, a slug naming the theme, and intents drawn only from the op vocabulary in `zelador/write/contracts.py` (`OPS`). One intent group per user-facing decision; keep unrelated themes in separate changesets so a rejection never drags down approved work.
- **Validate**: `uv run zel validate <changeset> --json`. Fix failures in the changeset, never the plan – plans are version-pinned artifacts, not editable files.
- **Approve in chat, per intent group**: show each group as the plan expanded it – objects touched, old → new, risk tier – and get an explicit yes/no per group. Any rejection: trim the changeset, re-validate, re-present.
- **Apply**: `uv run zel apply <plan id> --dry-run` first, always, and show the user what it prints; then `uv run zel apply <plan id>`. Oversized plans refuse without `--big` (threshold in `zel apply --help`) – treat that as a prompt to split, not a flag to reach for.
- **Confirm**: apply verifies its own writes; relay the outcome and the session id, and remind the user that `uv run zel undo <session> --dry-run` previews the rollback while regret is cheap.

Close with `uv run zel status` – no pending entries, fresh audit stamp – and a summary of what changed, grouped by theme, with session ids for undo.
