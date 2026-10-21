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

The snapshot was transferred through a temporary client-side-encrypted object-
store transport and verified by complete decrypted SHA-256 on the storage host.
The VPS snapshot, transport ciphertext and per-transfer keys were deleted after
verification. The source host again had 116 GiB available; the retained external
acceptance directory occupied 26 GiB before the full-size run.

The exact stock Hermes revision was fetched from upstream and checked out clean
on the external acceptance volume. Full-size acceptance is **not yet claimed**.
The durable run must produce its checkpointed `acceptance.json`; real rows,
per-month sizes, compression, replay results and trim timings must be copied from
that receipt, never inferred from the synthetic suite.

## Remaining gates

- Full-scale admission and completion: the delegated helper's native submit
  returned HTTP 409 `heavy admission refused`, as expected for a child agent.
  No heavy job was admitted or run. The owning parent thread must submit the
  staged exact spec through the native tool; do not bypass the queue with SSH.
  Then review the runner's actual receipt.
- Verified permanent encrypted R2 publication of the sealed months + catalog
  (the existing local-only helper cannot archive an arbitrary remote directory).
- Verified production reverse-SSH route; a trial with the unprovisioned alias
  correctly failed host-key validation. No validation was bypassed.
- Fresh frozen review of the final corrections and remote cutover/rollback.
- GitHub-hosted CI is blocked before any test step by the organization's billing/
  spending-limit gate. The Actions annotation explicitly says the job did not
  start; this is not a failing synthetic test. No billing changes were attempted.
- Owner-approved physical-erasure/resealing policy and handling of edits to
  already retired rows. v0 fails closed rather than guessing.

This is a runnable preview increment, **not cutover-ready** until those gates
are closed. The live Hermes store remains untouched.

## Full-size replay result (20 Oct 2026, Thales copy, commits c0997b7 → 274ac24)

- Artifacts: source and trimmed copies match the rehearsal checksums; `quick_check` ok; 749,116 active rows identical; the five longest old human threads (31,376 / 6,767 / 6,528 / 4,323 / 4,117 visible rows) display identically through stock paging.
- 1,153 real `session_search` calls from the last 30 days replayed with stock Hermes 818c13be1d on both stores.
  - 1,144 return the same top-K sessions. 9 differ by one swap at the last rank. In all 5 cases left after History, the dropped session is still returned by the trimmed store at rank K+1 (BM25 statistics shift when inactive rows leave). No session lost coverage.
  - 56 timeouts under a shared 60 s cap: 42 on the original store; 14 attributed to the trimmed store only because the original consumed 37–47 s of the cap first. Re-run alone, all 14 finish on the trimmed store in 0.4–27 s.
- Defect fixed: History search rejected Hermes query syntax (`OR`, quotes, `:`), raising "invalid FTS query". It now uses Hermes' sanitizer (vendored, MIT); regression test added (35 tests pass on VPS and Thales).
- Size: 27.5 GB → 20.2 GB on the copy (−27%), because every active row and all visible prose stay at every age.

## Hourly copy live (21 Oct 2026, a26e4eb)

- `agent-history-sync.timer` on the VPS runs hourly; the VPS dials Thales (`ops/bridge.py`), Thales needs no route back. Source reader runs at nice 19 / idle I/O, unit MemoryHigh=2G. First catch-up 885 s; 3,144,247 rows held.
- Fixed: a row inserted 21 Oct carrying a 30 Sept timestamp stopped the first run ("late insert in sealed month"). Late arrivals and newer versions of sealed rows now go to the open month; sealed files never change (September hash re-checked). No reseal needed.

## Independent trim review (Claude Fable, 21 Oct): SHIP WITH FIXES — trim not scheduled

1. Old compacted tool rows feed the Chats "Worked" receipt (step timing, plan, questions). Dropping them changes old threads. Old inactive rows measured on the 19 Oct copy: compacted tool rows 1,066,841 / 3.67 GB content; rewind tool rows 23,676 / 64 MB; compacted assistant reasoning 1.50 GB. Fix: keep every compacted tool row, shorten only its content, and prove the receipt is unchanged.
2. 1,942,338 visible rows (6,143 sessions) have no display identity yet; Hermes fills them on first view. Verify/trim must treat display_order/display_identity as display state, not content, or the window's verify fails closed.
3. VPS→Thales measures 1.4 MB/s (Thales→VPS 20 MB/s): shipping the 27.5 GB store to Thales inside a window takes ~6 h. Trim must run on the VPS against History's digest list.
4. Smaller: set WAL on the output, ANALYZE, refuse a hot -journal, assert max(id) kept, accept-trim through the stdio config, suspend the timer in the window.

## Decision point (21 Oct 2026): trim stopped

First-principles re-check with Claude Fable: the trim saves ~27% (about three weeks of growth at ~370 MB/day), needs a downtime window each time, and changes old threads' Worked receipts. Preservation is already met by the History copy plus R2. Trim work stops here. Sync cadence moved from hourly to daily 20:30 UTC (after the nightly backup) until something reads History.
Payload by source (19 Oct copy, all time / last 30 d, GB): api_server 3.20/2.49, webui 3.61/0.00, subagent 2.28/1.13, telegram 1.49/0.04, cron 1.34/0.39; machine sources ≈33% of payload, ≈38% of recent. Next lever, pending owner approval: Hermes' own `sessions prune --source cron --older-than 30` after History verifies, then subagent once lineages are checked against visible threads.
