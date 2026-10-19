# Delivery status

## Implemented and exercised

- Dependency-free Python 3.14 monthly compressed history, catalog, two FTS5
  indexes per month, metadata/flag/deletion reconciliation and JSONL export.
- CLI, newline-delimited MCP stdio, bounded session reads and SSH source RPC.
- Quiescent sealing and actual-byte per-session verification.
- Copy-only new-store trimmer, source-schema FTS rebuild, integrity checks and
  generation-bound acceptance ledger; no automatic live swap.
- Synthetic regressions for sync, compaction flags, deletions before/after trim,
  encrypted-reasoning exclusion, compression, cross-month search, pagination,
  export, sealing, hash refusal, stale trim acceptance, concurrent WAL writes,
  display-key calls, source RPC and MCP.
- A read-only independent implementation review; addressed safety findings and
  explicitly recorded the remaining production limitations in FINDINGS.

## Real-data staging

A read-only SQLite online backup produced a **27,475,886,080-byte** snapshot in
**139.007 seconds**. An initial unpinned backup was interrupted because active
writers could restart copying; the helper was corrected to pin the explicit
snapshot (hourly sync still uses short reads). No production database, WAL,
configuration, gateway or timer was changed.

The exact stock Hermes revision was fetched from upstream and checked out clean
on the external acceptance volume. Full-size acceptance is **not yet claimed**.
The durable run must produce its checkpointed `acceptance.json`; real rows,
per-month sizes, compression, replay results and trim timings must be copied from
that receipt, never inferred from the synthetic suite.

## Remaining gates

- Full-scale run completion and review of its actual results.
- Verified permanent encrypted R2 publication of the sealed months + catalog
  (the existing local-only helper cannot archive an arbitrary remote directory).
- Verified production reverse-SSH route; a trial with the unprovisioned alias
  correctly failed host-key validation. No validation was bypassed.
- Fresh frozen review including the SSH adapter, remote cutover and rollback.
- Owner-approved physical-erasure/resealing policy and handling of edits to
  already retired rows. v0 fails closed rather than guessing.

This is a runnable preview increment, **not cutover-ready** until those gates
are closed. The live Hermes store remains untouched.
