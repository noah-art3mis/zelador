---
name: zelador-setup
description: First-run onboarding for zelador — Zotero API key, .env, connection check, taxonomy.yaml opt-in. Use when the user invokes /zelador-setup or asks to set up zelador or connect their Zotero library.
---

# zelador setup

First-run onboarding. Open with `uv run zel status --json` — on a fresh clone most of it reads "none"/"no"; that is expected, skip what cannot exist yet.

1. **Credentials.** If status shows the API unreachable for missing credentials:
   - Have the user create an API key at zotero.org → Settings → Security → Applications (library read now; write access is needed from M3 on). Their user ID is shown on the same page.
   - Default flow: the user copies `.env.example` to `.env` and fills `ZOTERO_API_KEY` and `ZOTERO_USER_ID` themselves, so the key never enters the chat. If they prefer to paste the key for you to write, accept it — note it then enters the transcript. Never read `.env` back.
2. **Verify.** `uv run zel debug whoami` — confirm the username is theirs and access includes library read (and write). `uv run zel debug paths` — confirm the Zotero data dir was discovered; if not, copy `config.example.yaml` to `config.yaml` and set `zotero_data_dir`.
3. **Taxonomy opt-in.** Copy `taxonomy.example.yaml` to `taxonomy.yaml`, then walk the user through it family by family: keep or drop each family, adjust tags, aliases, and colours (any `#RRGGBB` hex, at most 9 coloured, order = Zotero selector position; the shipped Okabe-Ito hues are a recommended default, not a rule). For a vocabulary designed from real audit data, hand over to the `zelador-taxonomy` skill.
4. **Recommend** disabling Zotero's "Automatically tag items with keywords and subject headings" (desktop Settings → General) so the tag mess stops growing.
5. Finish with `uv run zel status` — everything it reports should now be present or explained.
