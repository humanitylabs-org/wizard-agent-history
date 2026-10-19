# Agent History v0

A permanent, searchable copy of one owner-authorized agent's history, independent
of its live harness store. Python **3.14+**, SQLite FTS5, standard library only at
runtime (`compression.zstd`). No gateway/plugin/core changes are required.

**Preview, not a live cutover.** The trimmer writes a new file from a quiescent
copy. It never swaps a live store, starts/stops services, or installs timers.
The production cutover gates in [OPERATIONS](docs/OPERATIONS.md) must pass first.

## Try it

From this checkout (no installation necessary):

```sh
python3 -m unittest discover -s tests -v
python3 -m agent_history --config config.local.json sync
python3 -m agent_history --config config.local.json sync --reconcile
python3 -m agent_history --config config.local.json verify
python3 -m agent_history --config config.local.json search 'garden'
python3 -m agent_history --config config.local.json search 'tool_name' --deep
python3 -m agent_history --config config.local.json read SESSION_ID --limit 100
python3 -m agent_history --config config.local.json seal YYYY-MM
python3 -m agent_history --config config.local.json stats
python3 -m agent_history --config config.local.json mcp
```

Copy `config.example.json` into an **untracked**, private configuration file and
set its source/root paths. The root contains `<agent>/catalog.db`, monthly
`YYYY-MM.db` files and `conversation.jsonl`. Each month has its own conversation
and deep FTS5 index. Sealed months remain searchable without rehydration.

To keep all history bytes off a small source host, run the CLI on the storage
host with an SSH read-only source descriptor (see `config.remote.example.json`).
Source reads travel through a short-lived stdio process with `mode=ro`,
`query_only=ON`, `nice 19`, idle ionice, and bounded ID reads. **Never mount a live
SQLite database over SSHFS/NFS.** Run searches/MCP on that same storage host via
SSH; no inbound network listener is opened.

## What is preserved

- Every message column, including inactive/compacted rows, display identities,
  API content, calls, tool output and readable reasoning; all session metadata
  and the system-prompt lookup table.
- Provider `encrypted_content` fields are removed recursively from structured
  payloads. Opaque reasoning-detail `signature` fields are excluded. Readable
  text is retained; malformed structured opaque metadata fails closed.
- Large canonical row payloads use zstd only when smaller. Contentless indexes
  contain searchable tokens, not a second full plain-text canonical copy.
- Incremental message-ID ingestion, fresh session metadata and a complete
  bounded IDs/flags/deletion reconciliation every pass. `--reconcile` also
  rereads old payloads, mandatory before sealing.
- Each successful sync atomically regenerates the owner-conversation JSONL:
  human surface sources, user messages and final assistant prose, excluding
  tool-call commentary, summaries and hidden/model-only rows. Deduplication
  uses durable message/display identity, falling back to role/time/content.
- Row-count and ordered content-hash proofs per session, checked against actual
  decompressed History bytes. Mutable visibility flags are checked separately.

## What trimming does

Keep **every active row, at every age, byte-for-byte** for provider continuation.
For inactive rows older than `live_days` (30 by default), drop tool-output rows
and clear reasoning payload fields. Keep every user/assistant prose row, all
display metadata, and **tool calls**, which participate in stock Hermes display
deduplication. Recent inactive rows are untouched too.

The new store uses the input's actual base-table, view, FTS and trigger DDL. FTS
indexes rebuild from their declared external-content views, not an assumed
`messages` projection. SQLite, foreign-key delta and external-content FTS
integrity checks run before output publication. A per-row trim ledger plus
catalog-generation binding prevents intentional trimming from being mistaken
for privacy deletions on the next sync.

## Interfaces and privacy boundaries

- MCP stdio: `sessions_search(query, deep, after, before, limit)` and
  `session_read(session_id, after_id, limit)`.
- Search uses FTS5 query syntax, UTC message-time bounds, and one newest matching
  message per session. This is **not** a promise to reproduce stock Hermes'
  BM25 ranking, title heuristics, session-start time bounds or lineage folding.
- One configured agent/root is the authorization boundary. No cross-agent
  search argument or private-agent grant mechanism is provided.
- Before sealing, privacy deletions remove source payload/index entries and are
  purged from free pages by the sealing VACUUM. After sealing, catalog tombstones
  immediately hide them from search/read/export; physical deletion from sealed
  files and prior encrypted backups requires an explicit reseal/erasure policy.
- A deliberately removed tool row cannot subsequently convey an individual
  owner deletion through an absent live row. Whole-session deletion propagates;
  deletion of surviving trimmed prose propagates. An archive-native deletion
  API is a remaining production requirement if individual retired-row erasure
  is desired.
- Edits to a sealed month or previously trimmed surviving row fail closed and
  require reconciliation. Automatic versioned resealing is not implemented.

## Acceptance and operations

[Evidence and source findings](docs/FINDINGS.md) ·
[Cutover/rollback runbook](docs/OPERATIONS.md) ·
[Delivery status](docs/STATUS.md)

The synthetic suite never reads operator state. `ops.prepare_acceptance` runs
an explicitly configured full-size rehearsal using a copied source, an isolated
stock-Hermes environment and private local evidence. Only aggregate receipts
are suitable for GitHub; never commit owner messages or search-query text.
