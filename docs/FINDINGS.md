# Findings and proposed spec amendments

Source inspection used deployed Hermes revision
`818c13be1dc4fd28987e1e881a9408224afd4535`. No tracked Hermes files were changed.
The isolated acceptance checkout fetched this exact revision directly from
`https://github.com/NousResearch/hermes-agent.git`, detached to it, and reported
no tracked changes. It is a stock, exact-deployed-revision compatibility check,
not an assertion about a different or newer release.

## Display is not active-only

- `wizard-ux/web/live.js:532` fetches
  `/wizard/hermes/api/sessions/{id}/messages?order=oldest&limit=500&offset=...&include_compacted=true&view=wizard`.
- `wizard-agent-service/server/service.mjs:128` admits this route/query; its
  `server/agents-http.mjs:565` also explicitly requests compacted history.
- `Hermes/gateway/platforms/api_server.py:3292-3296` forwards the query flag to
  `db.get_messages(..., include_compacted=..., include_ancestors=True)`.
- `Hermes/hermes_state_messages.py:1390-1400` distinguishes active provider
  context from display: compacted display uses `(active=1 OR compacted=1)`.
- `Hermes/hermes_state_messages.py:1409-1428` merges compression lineage and
  selects the indexed or legacy deduplicated display projection.

**Deleting all inactive rows would make old scrollback disappear.** Preserve
user/assistant prose, identity/order, metadata, UIDs and lineage session records.

## Continuation needs complete active rows

`Hermes/hermes_state_messages.py:1390-1391` makes normal reads active-only;
`get_messages_as_conversation` uses that same active-clause distinction.
Provider-specific continuation can require active tool-call pairs and opaque
provider context even when the session has not been used recently. Therefore
v0 retains *all* active rows and their original bytes, not just recent/open
sessions. The archive's encrypted-reasoning exclusion does not authorize
stripping that material from active live replay rows.

## Tool calls also affect scrollback identity

`Hermes/hermes_state_messages.py:1195-1196` includes tool calls in the fallback
display key. Lines `1207-1223` use that key to deduplicate generations, and
`1225-1256` can recompute persisted display identities. Merely keeping an old
`display_identity` blob while clearing calls is insufficient for lineage reads
or later repair. **v0 does not clear tool calls**, even on inactive old rows.
Reasoning fields are not part of this identity and can be removed after proof.

## Stock session_search uses the live store

- `Hermes/tools/session_search_tool.py:369-372` calls `db.search_messages`,
  defaulting to roles user/assistant and excluding kanban/subagent/tool sources.
- `Hermes/hermes_state_search.py:163-190` searches active **or compacted** rows,
  hides display-kind `hidden`, and applies bounds to **session start**, not
  message time.
- `Hermes/tools/session_search_tool.py:314-322` wraps ranked discoveries;
  title matching and lineage folding add semantics beyond raw FTS membership.

The actual source FTS contract is an 8-KiB tool-content projection view for
`messages_fts` and a non-tool filtered view for `messages_fts_trigram`. Rebuilding
both from every complete base row would silently violate that contract. v0
recreates source DDL and issues `rebuild` against the original declared views.

The full-scale runner replays actual stock `session_search` calls from the
last 30 days, freezing relative time bounds and comparing returned session sets.
Missing live results are checked against History. It records misses, additional
live results, tool errors and nondefault-profile skips rather than hiding them.
Exact top-K equality is a gate separate from archive content preservation.

## Proposed replacement for the Hermes live-retention sentence

> Hermes retains all active provider replay rows regardless of age, plus all
> user/assistant display rows and the fields needed to deduplicate them. After
> `live_days`, its trimmer may remove only verified inactive tool-output rows
> and verified inactive reasoning payloads. Tool calls remain while stock
> display identity depends on them. `ended_at` is not a retention clock.

This deliberately favors reliable scrollback/continuation over a fixed-size
30-day live database. A future thin-history display/rehydration adapter could
permit more aggressive shrinking, but requires separate product acceptance.

## Sealing, deletion and topology clarifications

1. A month's message timestamp determines its file, not session start. A
   long-running session can span files. The catalog is part of the archive and
   must be backed up with sealed files: it owns metadata, latest flags,
   tombstones and trim provenance.
2. A completed sync refreshes the readable export too; otherwise a deleted
   message would remain in an old JSONL even when database searches hide it.
3. Sealing means immutable payload files with a mutable visibility catalog.
   Pre-seal reconciliation physically removes deleted rows; post-seal physical
   erasure and old-backup retirement require a separately approved policy.
4. Deliberately retired rows need provenance. Without a trim ledger, the next
   deletion scan would erase the very history this system exists to preserve.
5. For this installation, the defensible layout is all monthly files and the
   catalog on the large storage host, not a network-mounted SQLite store and
   not an arbitrarily growing current month on the VPS. SSH source RPC reads
   the live source locally and returns bounded batches to the storage host.

## Independent review

A read-only independent Codex review found stale-ledger acceptance, concurrent
WAL mutation, provider-metadata parsing scope and ledger durability risks. The
implementation now binds acceptance to the exact catalog generation/watermark
and proof hash, pins copy-only rebuilds to one source transaction and rejects
concurrent data-version changes, rejects malformed opaque structured metadata,
and fsyncs the ledger. Synthetic regression tests exercise the first two risks,
post-trim privacy deletion, and preservation of display-key tool calls.

The reviewer also identified the intentionally limited post-seal physical
privacy policy and fail-closed post-trim edits. They are documented limitations,
not claims of completed archive-native erasure/revision support. The initial
review predates the later SSH-source adapter; that adapter needs a final frozen
review before production admission.
