# Agent History

Permanent, searchable monthly history for a single owner-authorized agent instance.

## Delivery contract

Consequential, copy-only preview: permanent-history correctness and future trimming
can lose private data if wrong. Direct implementation; independent read-only review
required before cutover. No live store writes, service restarts, scheduler installation,
or Hermes configuration changes are authorized by this repository's acceptance run.

Acceptance:
- read-only, incremental Hermes ingestion; readable payload preservation, opaque-provider
  exclusions, privacy deletions and compaction flags;
- conversation/deep search, bounded session reads, JSONL export and stdio MCP;
- verified copy-only rebuilding that preserves chat prose and every active replay row;
- synthetic regression suite plus a full-size isolated rehearsal and search replay;
- pushed source and a runbook, with unresolved gates explicitly reported.

Implementation and acceptance instructions follow on the feature branch.
