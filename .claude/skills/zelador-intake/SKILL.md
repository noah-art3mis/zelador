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
- **Duplicates** – a `--since` duplicate finding means the arrival collides with an existing item; surface it and let the user pick which to keep (trash the other, never delete). **Never propose a side before reading what hangs off each twin.** `zel undo` restores a trashed item's own record, so the reversible-by-design story holds for the item – but the choice of which twin to keep is what decides whose annotations, notes, and attachments go to the trash alongside it, and that loss stays invisible until the user goes looking for a highlight that is no longer there. Two checks, both cheap, before naming a keeper:
    - Children from the latest backup, no API cost – each line is `{"kind": "item", "object": {...}}` and every child carries `parentItem`, so a pass over the file maps parent → attachments, notes, and annotations.
    - Annotations the API cannot see, because they never synced – `uv run zel local "SELECT i.key, (SELECT COUNT(*) FROM itemAnnotations ia WHERE ia.parentItemID IN (SELECT a.itemID FROM itemAttachments a WHERE a.parentItemID = i.itemID)) AS annots, (SELECT COUNT(*) FROM itemNotes n WHERE n.parentItemID = i.itemID) AS notes FROM items i WHERE i.key IN (...)" --json`.
    A twin carrying annotations or notes is not a trash candidate – keep that side and move the other side's metadata onto it. The same rule decides the common preprint-meets-published collision: when the copy the user has read is the poorer bibliographic record, keep the read copy and raise it to the version of record with `set_item_type` and `fill_field`, rather than trashing their reading state to gain cleaner metadata. Report what the checks found, not just the recommendation – "no annotations or notes on any of them" is the sentence that earns the trash.

Push agreed fixes through the standard change loop, exactly as `zelador-review` does:

- Author a changeset in `<data dir>/changesets/<slug>.json` (`schema: changeset.v1`, ops from `OPS` in `zelador/write/contracts.py`), one intent group per user-facing decision.
- `uv run zel validate <changeset> --json`; fix failures in the changeset, never the plan.
- Approve where the risk is, reading the tier `expand` assigned and `zel validate` prints per group. The additive work of a normal intake – `topic:` tags from the registry, collection adds, fills into empty fields – is all `low`: apply it and report per item what each arrival received. The `high` groups wait for an explicit yes/no each, objects touched and old → new: trashing a duplicate twin, folding a tag, overwriting a field the item already had. A typical session earns one round trip, over the duplicates – not thirty over tag writes the registry already vouched for.
- `uv run zel apply <plan id> --dry-run` first, always; then `uv run zel apply <plan id>`.
- Relay the outcome and session id; `uv run zel undo <session> --dry-run` previews rollback while regret is cheap.

Close with `uv run zel status` – no pending entries, fresh stamps – and a per-item summary of what each arrival received.
