# Operations and cutover runbook

**Do not execute the live-maintenance section as part of v0 acceptance.** This
repository has only been authorized to copy/read production data and to trim
copies. Live installation, stopping writers, swapping stores, MCP registration
and scheduler installation require a subsequent approved cutover.

## Configuration and placement

Keep owner paths/hostnames in private config/environment files, never source.
Runtime requires Python 3.14+ and SQLite FTS5. The platform's default `python3`
may be older; pin the tested interpreter in configuration.

- Small control-plane checkout, configs and aggregate reports: source host.
- Catalog, **all** monthly files, export, test database and work caches: external
  storage volume on the storage host. Even the current month's deep index may
  exceed the source host's single-file/directory threshold.
- Sealed files plus a consistent catalog snapshot: encrypted object storage,
  in a permanent prefix with no object-expiry lifecycle rule.
- Do not place any work tree, virtualenv, cache or test DB on the storage host's
  internal system volume. Set `TMPDIR` and `UV_CACHE_DIR` explicitly.

`config.remote.example.json` runs on the storage host. Its `source` object opens
one authenticated SSH stdio reader on the source host. Both source process and
connection are read-only. No SSHFS/NFS SQLite mounts are involved. A reverse SSH
route with a pinned, independently verified host key is a production gate; do
not turn off host-key verification to make a new route work.

Search/MCP from the source host is a normal SSH invocation on the storage host:

```sh
ssh "$HISTORY_HOST" "cd '$REMOTE_REPO' && '$REMOTE_PYTHON' -m agent_history --config '$REMOTE_CONFIG' search 'example'"
ssh "$HISTORY_HOST" "cd '$REMOTE_REPO' && '$REMOTE_PYTHON' -m agent_history --config '$REMOTE_CONFIG' mcp"
```

The quoted values must be administrator-controlled paths, not user query text.
For programmatic callers, build SSH commands using `shlex.join`, as the source
adapter does. Register MCP later through the owner-approved Hermes workflow;
this repository makes no Hermes config changes.

## Normal maintenance

Run with one writer (the CLI holds an exclusive advisory catalog lock):

```sh
python3 -m agent_history --config "$CONFIG" sync
python3 -m agent_history --config "$CONFIG" sync --reconcile
python3 -m agent_history --config "$CONFIG" verify
python3 -m agent_history --config "$CONFIG" seal "$PAST_MONTH"
python3 -m agent_history --config "$CONFIG" stats
```

`sync` does short read transactions, refreshes metadata and all row flags,
propagates missing source IDs, then regenerates `conversation.jsonl`. Old payload
edits are picked up by full reconciliation. `seal` always reconciles and verifies
first; it will not seal the current/future month. Seal only a quiescent source
(stopped writer or immutable snapshot); an observed concurrent commit aborts sealing. The normal sync is incremental
in payload IO, not O(new rows) in metadata IO: a complete IDs/flags pass is
necessary because Hermes supplies no mutation/deletion changefeed.

The unit/timer files under `ops/` are **staged examples only**. No timer was
installed. Hermes auto-prune remains untouched/off. Do not enable it as a
substitute for the verified trimmer.

## Full-scale acceptance (copy only)

Prepare an untracked JSON config containing `test_root`, `source`, `root`,
`agent`, `live_days`, `hermes_checkout`, `hermes_revision`, and `uv`.
`source` must be a consistent copied DB on the storage host. The checkout must
contain unmodified deployed Hermes source at the recorded revision.

Before staging a snapshot on the source host:

```sh
date -u
df -h /
nice -n 19 ionice -c3 python3 ops/snapshot.py \
  --source "$LIVE_DB" --output "$SCRATCH_SNAPSHOT" --min-free-gib 80
```

The snapshot helper refuses the 21:00–00:30 UTC backup exclusion window and a
free-space floor violation. It uses SQLite's online backup API with a read-only
source. Unlike hourly sync, this **explicit full snapshot** pins one read
transaction so continuous source writes cannot restart the backup forever.
A failed snapshot is marked `.partial`; inspect/remove only that run-owned file.

Transfer the snapshot, compare its complete SHA-256 on both hosts, then delete
the source-host staging copy. SSH transfers may be slow. If a temporary
object-store transport is used, encrypt client-side before upload, send only a
per-transfer key over authenticated SSH, verify the decrypted destination hash,
and remove the exact temporary object and per-transfer keys afterward. Such a
transport is **not** a published permanent backup.

Stage the pushed source revision, then submit (do not background/poll a heavy
suite in a conversational terminal):

```text
wizard_heavy_submit(
  key=<stable intended-run key>,
  cwd=<external-volume checkout>,
  argv=[<python3.14>, "-m", "ops.prepare_acceptance", "--config", <private config>],
  timeout_seconds=28800,
  lane="heavy"
)
```

The runner installs the stock frozen dependency environment in the external
work root, verifies stock imports **before** the long data pass, then runs:

1. Synthetic tests and source checksum.
2. Full initial sync and a second incremental sync (including JSONL export).
3. Count/hash verification, explicit export, per-month/raw/compressed sizes.
4. Pre-seal reconciliation/verification and sealed-file hashes.
5. Copy-only new-file trim with source FTS contract and integrity checks.
6. Unmodified deployed `SessionDB` read-only opens, byte-for-byte hashing of all
   active rows, actual long-thread display projection comparison, and replay of
   recent `session_search` arguments. Private query strings never leave the
   private test evidence directory; aggregate counts are shareable.
7. Separate VACUUM timing and another SQLite/FTS integrity check. VACUUM changes
   file bytes, so the test ledger's exact output checksum is refreshed afterward.

`acceptance.json` is a checkpointed aggregate receipt. `evidence-private/` holds
query strings and diagnostics and **must never be committed**. A failed phase
is not a pass. Preserve its receipt and inspect the private error before
explicitly starting another intended run; never submit duplicates to poll.

## Required cutover gates

All must be true before stopping anything:

- Full-size receipt passes, with no replay skips/errors/misses and understood
  additional top-K sessions; active-row and old-thread projections match.
- The final source/SSH adapter and recovery path receive a fresh independent
  review. A prior review of an earlier commit is not production approval.
- Exact monthly/catalog bytes have an encrypted permanent copy whose remote
  bytes and off-host restore are verified. A temporary transfer object does
  not satisfy this gate.
- The reverse SSH source route is verified without weakening host-key checks.
- The source schema/FTS DDL still matches the rehearsal contract.
- Maintenance duration includes **snapshot + both transfers + final verify +
  rebuild + validation**, not just the atomic rename. The full-scale trim time
  is a lower bound, not an advertised gateway outage estimate.
- The owner has approved post-seal logical-vs-physical deletion semantics and
  the fail-closed handling of edits to already sealed/trimmed history.
- Free space remains above the source-host floor throughout. The original live
  DB will remain as a hard-linked rollback store until the next verified backup.

### Encrypted cold-storage helper limitation

Inspect its help before use:

```sh
python3 "$HOME/.hermes/scripts/r2_cold_storage.py" --help
python3 "$HOME/.hermes/scripts/r2_cold_storage.py" archive "$APPROVED_LOCAL_ARCHIVE_DIR"
```

The currently installed helper accepts a finite allowlist of **local** backup
folders (`safe_source`, lines 94–100). It does not accept an arbitrary external
storage-host directory or SSH URL. Do not pretend that the command archives a
remote month, silently change that helper, mount live SQLite over a network, or
stage an oversized permanent collection on the small VPS. A reviewed remote
streaming adapter / approved archival host is a remaining integration gate.
Only its verified permanent receipt allows retiring the rollback store.

## Approved live-maintenance procedure (later, not performed by v0)

An operator-owned, mode-0600 environment file supplies `LIVE_DB`,
`SCRATCH_SNAPSHOT`, `HISTORY_HOST`, `REMOTE_REPO`, `REMOTE_PYTHON`, `REMOTE_CONFIG`,
`REMOTE_SNAPSHOT`, `REMOTE_TRIMMED`, `LOCAL_NEW`, `ROLLBACK_DIR`, and the Bash array
`WRITER_UNITS`. The paths are deliberately not checked into source.

1. **Close admission and stop every discovered writer.** Finish active turns,
   suspend the future History timer, and inventory all open DB/sidecar holders.
   The observed installation includes gateway, dashboard, standalone API and a
   terminal-hosted Hermes surface; gateway-only stopping is insufficient.
   Resolve which units/processes actually write this store before approving the
   writer list. Do not stop unrelated services or kill unknown PIDs.

   ```sh
   systemctl --user stop "${WRITER_UNITS[@]}"
   systemctl --user is-active "${WRITER_UNITS[@]}"  # all must be inactive
   lsof "$LIVE_DB" "$LIVE_DB-wal" "$LIVE_DB-shm"  # require no holders
   ```

   Record SHA-256 for the stopped main DB and any existing sidecars. Keep the
   gateway stopped through step 7. This is the maintenance-window start.

2. **Final History catch-up against the stopped source.** From the storage host:

   ```sh
   python3 -m agent_history --config "$CONFIG" sync --reconcile
   python3 -m agent_history --config "$CONFIG" verify
   ```

   Snapshot the catalog with SQLite's backup API to a rollback catalog file,
   not `cp` against a running writer. Close any readers before later replacement.

3. **Create and transfer the final consistent source snapshot.** Use the exact
   snapshot command above, outside the backup exclusion window. Verify transfer
   SHA-256 and delete VPS staging. Do not reuse the earlier rehearsal snapshot:
   it is already stale. On the storage host, verify this local snapshot against
   the same History catalog (the override need not change its bound source):

   ```sh
   python3 -m agent_history --config "$CONFIG" verify --source "$REMOTE_SNAPSHOT"
   python3 -m agent_history --config "$CONFIG" trim-copy \
     --source "$REMOTE_SNAPSHOT" --output "$REMOTE_TRIMMED" --live-days 30
   ```

   Require all integrity checks and source-proof checks to pass. Preserve
   `$REMOTE_TRIMMED.trim-ledger.db`. Do not sync History again between this trim
   and ledger acceptance: a changed catalog generation invalidates acceptance.

4. **Transfer the new file back**, into `LOCAL_NEW` on the same filesystem as
   `LIVE_DB`. Verify its complete SHA-256 against the trim receipt. Reconfirm
   zero DB holders and unchanged stopped-original main/sidecar hashes. Check
   free space again. Any mismatch aborts without replacing anything.

5. **Atomic replacement with the old inode retained.** After independently
   reviewing the supplied environment, this Python command keeps a hard link to
   the original, moves its matching sidecars aside, and atomically replaces the
   main pathname. All writers must still be stopped; both paths must be on the
   same filesystem. `ROLLBACK_DIR` must be a new private directory.

   ```sh
   python3 -c 'import os,pathlib; p=pathlib.Path(os.environ["LIVE_DB"]); n=pathlib.Path(os.environ["LOCAL_NEW"]); b=pathlib.Path(os.environ["ROLLBACK_DIR"]); b.mkdir(mode=0o700); os.link(p,b/"state.db"); [(p.with_name(p.name+s).replace(b/("state.db"+s))) for s in ("-wal","-shm") if p.with_name(p.name+s).exists()]; os.replace(n,p); f=os.open(p.parent,os.O_RDONLY); os.fsync(f); os.close(f)'
   ```

   If interruption occurs before replacement, the old main/sidecar set is in
   `ROLLBACK_DIR`. Never restart on a mismatched main/WAL pair; follow rollback.

6. **Accept provenance while stopped.** Run on the storage host with the
   production config's SSH source pointing at the new canonical live pathname:

   ```sh
   python3 -m agent_history --config "$CONFIG" accept-trim \
     --ledger "$REMOTE_TRIMMED.trim-ledger.db"
   ```

   It verifies the exact file checksum, catalog generation/watermark and proof
   hash, then records intentional retirements and the new source identity.
   Failure means rollback before starting writers, not a forced catalog edit.

7. **Start writers and verify the actual surfaces.**

   ```sh
   systemctl --user start "${WRITER_UNITS[@]}"
   systemctl --user is-active "${WRITER_UNITS[@]}"
   ```

   Read a preselected long old thread through Wizard's real authenticated
   messages endpoint, compare prose/ordering, and continue an old thread with
   a harmless authorized message. Run a new sync and recheck default/deep
   searches and export. This is the maintenance-window end only after success.
   Register MCP/enable the hourly unit separately through the approved config
   workflow. Do not enable Hermes' own ended-session prune.

8. **Retain rollback until the next backup verifies.** Verify an off-host restore
   of the new live store and permanent History copy. Only then approve removal
   of the old inode and its exact sidecars. Large rollback data belongs in
   encrypted object storage, not indefinitely on the VPS.

### Rollback

Stop the same writers and suspend History sync. Quarantine the new main DB and
its *new* sidecars together (they may contain post-cutover turns; do not delete
them). Restore the old hard-linked main and its matching sidecars from
`ROLLBACK_DIR`, restore the pre-acceptance catalog snapshot, fsync directories,
then start writers. If post-cutover turns exist, preserve and reconcile them
before any retry. A catalog from the new trim must not be paired with the old
untrimmed source without an explicit reconciliation. No automatic rollback or
live rename is implemented by the CLI.
