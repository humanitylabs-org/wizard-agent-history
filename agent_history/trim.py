"""Copy-only Hermes rebuilding. Never mutates its input or performs a live swap."""
from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from .store import (accumulate, batches, canonical, file_hash, finished, ro,
                    row_hash, sanitize, unjson)

# tool_calls participates in Hermes' display deduplication identity. Retain it:
# clearing it splits compacted/live copies and can duplicate prose on scrollback.
STRIP = ('reasoning', 'reasoning_content', 'reasoning_details', 'codex_reasoning_items')


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def guard_copy(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    forbidden = {(Path.home() / '.hermes/state.db').resolve()}
    if os.getenv('HERMES_HOME'):
        forbidden.add((Path(os.environ['HERMES_HOME']) / 'state.db').resolve())
    if source in forbidden or output.resolve() in forbidden:
        raise ValueError('live/default Hermes store is forbidden; use a verified snapshot')
    if output.exists() or output.is_symlink() or source == output.resolve():
        raise ValueError('output must be a new file distinct from source')
    if Path(str(source) + '-wal').exists() and Path(str(source) + '-wal').stat().st_size:
        raise ValueError('copy must be quiescent: nonempty WAL present')
    return source, output


def trim_row(row, cutoff):
    """Age is per row; all active replay rows remain byte-identical at every age."""
    if row.get('active', 1) or row['timestamp'] >= cutoff:
        return row
    if row['role'] == 'tool':
        return None
    new = dict(row)
    for key in STRIP:
        if key in new:
            new[key] = None
    return new


def trim_copy(history, source, output, live_days=30, now=None):
    if live_days < 1:
        raise ValueError('live_days must be positive')
    source, output = guard_copy(source, output)
    start = time.monotonic()
    cutoff = (time.time() if now is None else now) - live_days * 86400
    before_stat = source.stat()
    proofs = {r['session_id']: {'count': r['row_count'], 'sha256': r['content_hash']}
              for r in history.cat.execute('SELECT * FROM proofs')}
    if not proofs:
        raise ValueError('no verified per-session History proof; run verify first')
    building = output.with_name(output.name + '.building')
    ledger_path = output.with_name(output.name + '.trim-ledger.db')
    if building.exists() or ledger_path.exists():
        raise ValueError('stale build/ledger present; inspect before retry')
    counts = dict(source_rows=0, kept_rows=0, dropped_rows=0, stripped_rows=0)
    actual = {}
    try:
        with contextlib.closing(ro(source)) as src, contextlib.closing(sqlite3.connect(building)) as dst, contextlib.closing(sqlite3.connect(ledger_path)) as ledger:
            dst.execute('PRAGMA journal_mode=DELETE')
            dst.execute('PRAGMA synchronous=FULL')
            dst.execute('PRAGMA temp_store=FILE')
            dst.execute('PRAGMA cache_size=-32768')
            ledger.execute('CREATE TABLE retired(id INTEGER PRIMARY KEY,expected_digest TEXT)')
            ledger.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            schema = src.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
            shadows = {r['name'] for r in src.execute('PRAGMA table_list').fetchall() if r['type'] == 'shadow'}
            virtual = [r for r in schema if r['type'] == 'table' and r['sql'].upper().startswith('CREATE VIRTUAL TABLE')]
            for r in virtual:
                if not r['name'].startswith('messages_fts') or 'using fts5' not in r['sql'].lower():
                    raise ValueError('unsupported virtual-table contract')
            tables = [r for r in schema if r['type'] == 'table' and r['name'] not in shadows
                      and r not in virtual and not r['name'].startswith('sqlite_')]
            for r in tables:
                dst.execute(r['sql'])
            for r in tables:
                name = r['name']
                cols = [x['name'] for x in src.execute(f'PRAGMA table_info({quoted(name)})')]
                insert = f'INSERT INTO {quoted(name)} ({",".join(map(quoted,cols))}) VALUES ({",".join("?" for _ in cols)})'
                if name == 'messages':
                    for batch in batches(src):
                        kept = []
                        for raw in batch:
                            row = dict(raw)
                            safe = sanitize(row)
                            digest = row_hash(safe)
                            accumulate(actual, safe, digest)
                            counts['source_rows'] += 1
                            new = trim_row(row, cutoff)
                            if new != row:
                                inv = history.cat.execute('SELECT * FROM inventory WHERE id=? AND deleted=0', (row['id'],)).fetchone()
                                if inv is None or inv['digest'] != digest:
                                    raise ValueError('refusing unverified changed row')
                                # Re-hash actual stored bytes, not just the catalog's claim.
                                if row_hash(history.stored(inv)) != digest:
                                    raise ValueError('History payload changed after verification')
                                ledger.execute('INSERT INTO retired VALUES (?,?)',
                                               (row['id'], row_hash(sanitize(new)) if new else None))
                            if new is None:
                                counts['dropped_rows'] += 1
                                continue
                            counts['stripped_rows'] += int(new != row)
                            counts['kept_rows'] += 1
                            kept.append(tuple(new.get(k) for k in cols))
                        dst.executemany(insert, kept)
                        dst.commit()
                        ledger.commit()
                else:
                    cur = src.execute(f'SELECT * FROM {quoted(name)}')
                    while rows := cur.fetchmany(400):
                        dst.executemany(insert, [tuple(row) for row in rows])
                        dst.commit()
            if finished(actual) != proofs:
                raise ValueError('source per-session counts/hashes changed since verification')
            # AUTOINCREMENT must not reuse a retired high message ID.
            if src.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                dst.execute('DELETE FROM sqlite_sequence')
                dst.executemany('INSERT INTO sqlite_sequence VALUES (?,?)',
                                [tuple(r) for r in src.execute('SELECT * FROM sqlite_sequence')])
            for r in schema:
                if r['type'] == 'view':
                    dst.execute(r['sql'])
            for r in virtual:
                dst.execute(r['sql'])
                name = quoted(r['name'])
                dst.execute(f"INSERT INTO {name}({name}) VALUES ('rebuild')")
                dst.commit()
                dst.execute(f"INSERT INTO {name}({name},rank) VALUES ('integrity-check',1)")
            for r in schema:
                if r['type'] == 'index' and r['name'] not in shadows:
                    dst.execute(r['sql'])
            # Indexes are now complete, not partially rebuilt. Preserve all other metadata.
            if any(r['name'] == 'state_meta' for r in tables):
                dst.execute("DELETE FROM state_meta WHERE key IN ('fts_rebuild_high_water','fts_rebuild_progress',"
                            "'fts_cjk_rebuild_high_water','fts_cjk_rebuild_progress','fts_stale','fts_cjk_stale')")
            for r in schema:
                if r['type'] == 'trigger':
                    dst.execute(r['sql'])
            dst.commit()
            problems = [r[0] for r in dst.execute('PRAGMA integrity_check') if r[0] != 'ok']
            if problems:
                raise ValueError('rebuilt SQLite integrity failed')
            # Existing source FK violations are not silently assigned to this change.
            before_fk = {tuple(r) for r in src.execute('PRAGMA foreign_key_check')}
            after_fk = {tuple(r) for r in dst.execute('PRAGMA foreign_key_check')}
            if after_fk - before_fk:
                raise ValueError('rebuilt store introduced foreign-key violations')
            dst.execute('PRAGMA user_version=' + str(src.execute('PRAGMA user_version').fetchone()[0]))
            dst.commit()
            ledger.executemany('INSERT INTO meta VALUES (?,?)', [
                ('schema', '1'), ('history_agent', history.agent),
                ('history_root', str(history.dir)), ('source_path', str(source)),
                ('cutoff', str(cutoff))])
            ledger.commit()
        after_stat = source.stat()
        if (before_stat.st_size, before_stat.st_mtime_ns) != (after_stat.st_size, after_stat.st_mtime_ns):
            raise ValueError('source changed while rebuilding')
        # A compact new file avoids the extra full-sized temporary VACUUM copy.
        with open(building, 'rb') as f:
            os.fsync(f.fileno())
        digest = file_hash(building)
        with sqlite3.connect(ledger_path) as ledger:
            ledger.execute('INSERT INTO meta VALUES (?,?)', ('output_sha256', digest))
        os.replace(building, output)
        dfd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return dict(counts, source_bytes=before_stat.st_size, output_bytes=output.stat().st_size,
                    seconds=round(time.monotonic() - start, 3), output_sha256=digest,
                    ledger=str(ledger_path), baseline_fk_violations=len(before_fk))
    except BaseException:
        # Keep failed owned files for diagnosis; never publish output on failure.
        raise


def accept_trim(history, source, ledger_path):
    """Explicitly bind intentional source reduction to its verified copy ledger."""
    with contextlib.closing(ro(ledger_path)) as ledger:
        meta = dict(ledger.execute('SELECT key,value FROM meta').fetchall())
        if meta.get('history_root') != str(history.dir) or meta.get('history_agent') != history.agent:
            raise ValueError('trim ledger belongs to another history')
        if file_hash(source) != meta.get('output_sha256'):
            raise ValueError('source is not the exact verified trim output')
        for r in ledger.execute('SELECT * FROM retired'):
            history.cat.execute('INSERT OR REPLACE INTO retired VALUES (?,?)', tuple(r))
        history.set('source', str(Path(source).resolve()))
        st = Path(source).stat()
        history.set('source_identity', f'{st.st_dev}:{st.st_ino}')
        history.cat.execute('DELETE FROM proofs')
        history.cat.commit()
    return {'accepted': True}
