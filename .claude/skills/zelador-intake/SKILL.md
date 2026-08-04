---
name: zelador-intake
description: Steady-state intake session – triage only the items that arrived since the last backup, tag them from the registry, fill metadata gaps, through the standard change loop. Use when the user invokes /zelador-intake, wants to triage recent arrivals, or asks to process what's new in their Zotero library.
---

# zelador intake

The steady-state session: the same loop as `zelador-review`, scoped to the delta. Cleanup gave way to intake – each session triages what arrived since the last one so new items stay conformant and the library never needs a second campaign. No new machinery: this skill is a scoping discipline over the same commands.

Orientation (mandatory, in order):

1. `uv run zel status --json` – refuse to continue past unresolved pending sessions (`zel debug reconcile` first). The previous backup's `library_version` is the **marker** – record it before anything moves it.
2. `uv run zel backup` – a verified no-op when nothing changed; everything downstream pins to this snapshot. If the live version already equals the marker, nothing arrived – report that and stop.
3. `uv run zel audit --since <marker>` – findings for arrivals only; read the report and per-check JSON in `<data dir>/audit/` (paths from `zel debug paths`).
4. `uv run zel items --since <marker> --json` – the arrivals themselves; read `taxonomy.yaml` so every tag proposal targets a registered canonical. No registry yet? Recommend the `zelador-taxonomy` skill first.

Then triage each arrival with the user. For every new item propose, from the audit findings and the item itself:

- **Workflow state** – nothing, ever. `status:` tags are assigned manually by the user only; never propose or apply `status:to-read` (or any other `status:`) yourself – a blanket to-read pass makes the tag meaningless. At most, point out arrivals that lack one.
- **Topics** – `topic:` tags drawn from the registry; a genuinely new subject is a taxonomy conversation (add the canonical to `taxonomy.yaml` with the user, subtopic + broad tag alongside), not an excuse for an unregistered tag. Topics carry the whole subject judgement – see collections below.
- **Collections** – only three are still filed into: **Projects ▸ CAPTA ▸ Public Opinion**, **Courses**, and **Projects ▸ Software** (keys from `zel collections`). Everything else in the tree is legacy, kept for the items already in it and closed to arrivals – the M5 conversion moved subject grouping to the `topic:` registry, and filing a new item under the old subject hierarchy re-creates the duplication that conversion removed. A subject with no registered canonical is a taxonomy conversation, never a new collection.
- **Metadata gaps** – completeness findings filled via `uv run zel lookup crossref KEY` (or `arxiv`); `uv run zel lookup fulltext KEY` when the PDF itself must be read. Candidates are proposals like any other – never auto-accepted.
- **Duplicates** – a `--since` duplicate finding means the arrival collides with an existing item; surface it and let the user pick which to keep (trash the other, never delete).

Push agreed fixes through the standard change loop, exactly as `zelador-review` does:

- Author a changeset in `<data dir>/changesets/<slug>.json` (`schema: changeset.v1`, ops from `OPS` in `zelador/write/contracts.py`), one intent group per user-facing decision.
- `uv run zel validate <changeset> --json`; fix failures in the changeset, never the plan.
- Approve in chat per intent group – objects touched, old → new, 3–5 sample titles, risk tier – explicit yes/no each.
- `uv run zel apply <plan id> --dry-run` first, always; then `uv run zel apply <plan id>`.
- Relay the outcome and session id; `uv run zel undo <session> --dry-run` previews rollback while regret is cheap.

Close with `uv run zel status` – no pending entries, fresh stamps – and a per-item summary of what each arrival received.
