# zelador

A deterministic Python CLI over the Zotero Web API for reading, auditing, and reorganizing a personal Zotero library. The agent (Claude Code) proposes; deterministic code validates; the user approves; every applied change is logged and reversible. Full design in [SPEC.md](SPEC.md).

## Install

The repo is the install unit — there is no published package:

```bash
git clone https://github.com/noah-art3mis/zotero-driver
cd zotero-driver
uv sync
mkdir -p ~/.config/zelador   # the config dir on Linux; `uv run zel debug paths` prints yours
cp .env.example ~/.config/zelador/.env                        # fill in your Zotero API key and user ID
cp config.example.yaml ~/.config/zelador/config.yaml          # optional
cp taxonomy.example.yaml ~/.config/zelador/taxonomy.yaml      # optional – the tag registry; edit to taste
uv run zel --help
```

## Commands

The CLI is self-documenting — the authoritative command surface, grouped by panel, lives in its help output:

```bash
uv run zel --help           # everyday verbs: library reads, findings, the change loop
uv run zel debug --help     # inspection and recovery utilities
uv run zel apply --help     # every command carries an Examples block
```

Data (backups, audit output, plans, logs) lives in the platform user data dir (`~/.local/share/zelador/` on Linux), overridable with `ZELADOR_DATA_DIR` — never inside this repo.
