---
name: zelador-taxonomy
description: Design or revise taxonomy.yaml interactively from real audit data — cluster existing tags into a canonical family:value vocabulary with aliases and colours. Use when the user invokes /zelador-taxonomy or wants to design or revise their tag taxonomy.
---

# zelador taxonomy design

Design or revise the registry (`taxonomy.yaml`) from real audit data — never invent a vocabulary up front. This is an interactive session: propose, let the user decide, record.

Orientation (mandatory, in order):

1. `uv run zel status --json` — confirm a recent backup and audit exist; run `uv run zel backup` and `uv run zel audit` first if stale.
2. Read the tag-mess audit JSON (`<data dir>/audit/tags.json` — path from `zel debug paths`) and the full tag list with counts: `uv run zel tags --json`.
3. Read the current `taxonomy.yaml` if present; otherwise start from a fresh copy of `taxonomy.example.yaml`.

Then work the vocabulary with the user, in passes:

- **Workflow families first** (`status:`, `rating:` and kin): map the user's existing workflow tags to canonical values and record the old names as aliases. These are the coloured families — assign colours ordered by frequency of use (order = Zotero number-key position, at most 9 coloured tags). Any `#RRGGBB` hex is valid; if the user already has colours in Zotero, record those. Absent a preference, reach for the Okabe-Ito colourblind-safe palette the example registry ships, and skip its yellow.
- **Topic vocabulary**: cluster the audit's near-duplicate groups and the highest-count tags into candidate `topic:` values; propose in batches, let the user accept, rename, or drop; record every absorbed spelling as an alias so `merge_tag` changesets can fold them later. The low-count tail can stay unregistered — the registry conformance check keeps reporting it, and deciding its fate is M4's batch work.
- **New families** only when the audit shows a real need — the family list lives in the registry, not in code.

Rules the registry linter enforces (load fails loudly otherwise): lowercase `family:value` names, declared families only, no duplicate canonical tags, no alias under two tags or shadowing a canonical, well-formed `#RRGGBB` colours and only in `coloured` families, at most 9 coloured tags. An `exclusive` family allows one tag of that family per item.

Record decisions directly in `taxonomy.yaml` as they are made. Finish with `uv run zel audit registry` and walk the user through the conformance counts — unknown tags, untriaged items, exclusivity violations, and colour drift are the work the registry has just made visible, not problems to fix in this session.
